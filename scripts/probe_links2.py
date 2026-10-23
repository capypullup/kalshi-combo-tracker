"""One-off probe, round 2: which kalshi.com link opens one specific combo? Runs in CI only.

The iOS app claims kalshi.com/markets/*, /events/*, /markets_by_ticker/*, /share/*, /t/* (not /combos).
On the web, /markets/<combo ticker> just lands on the /combos list. This checks:
1. /markets_by_ticker/<combo> and /markets_by_ticker/<leg> on the web.
2. What a combo card on /combos links to, and what URL a tap on one leads to.
3. Context around combo / share / ticker routes in the site's JS.
4. Android app links for kalshi.com (Google's cached Digital Asset Links).
"""

import json
import re
import sys
import traceback
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from combo_tracker.client import COMBO_PREFIX, KalshiClient  # noqa: E402

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.5 Mobile/15E148 Safari/604.1")


def section(title):
    print(f"\n========== {title} ==========", flush=True)


section("Android app links (Google Digital Asset Links)")
for site in ["https://kalshi.com", "https://www.kalshi.com", "https://app.kalshi.com"]:
    try:
        r = requests.get("https://digitalassetlinks.googleapis.com/v1/statements:list",
                         params={"source.web.site": site, "relation": "delegate_permission/common.handle_all_urls"},
                         timeout=20)
        print("--", site, r.status_code)
        print(r.text[:1500])
    except Exception as exc:
        print("--", site, "ERR", repr(exc))

section("A live combo from the API")
client = KalshiClient()
combo = leg_market = None
try:
    page = client._get("/markets/trades", {"limit": 1000})
    tickers = [t["ticker"] for t in page.get("trades", []) if t["ticker"].startswith(COMBO_PREFIX)]
    if tickers:
        combo = client._get(f"/markets/{tickers[0]}")["market"]
        legs = combo.get("mve_selected_legs") or []
        print("combo", combo["ticker"], "event", combo["event_ticker"], "legs", json.dumps(legs)[:800])
        if legs:
            leg_market = legs[0]["market_ticker"]
except Exception:
    traceback.print_exc(file=sys.stdout)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright not installed")
    sys.exit(0)


def settle(page):
    for _ in range(15):  # the Vercel checkpoint reloads itself once it passes
        title = page.title() or ""
        if "Checkpoint" not in title and "Security" not in title:
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(4000)


def show(page, url, label=None):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        settle(page)
        text = page.inner_text("body")[:700].replace("\n", " | ")
        print(f"-- {label or url}\n   status {resp.status if resp else None} -> {page.url}\n   title {page.title()!r}\n   text {text}")
    except Exception as exc:
        print(f"-- {label or url}\n   ERR {exc!r}"[:400])


js_urls, api = set(), []
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(user_agent=UA, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="en-US")
    page = ctx.new_page()

    def on_response(r):
        if r.url.endswith(".js"):
            js_urls.add(r.url)
        elif "combo" in r.url.lower() or "multivariate" in r.url.lower():
            api.append(r)

    page.on("response", on_response)

    section("markets_by_ticker on the web")
    if combo:
        t = combo["ticker"]
        for url in [f"https://kalshi.com/markets_by_ticker/{t}", f"https://kalshi.com/markets_by_ticker/{t.lower()}",
                    f"https://kalshi.com/markets_by_ticker/{combo['event_ticker']}"]:
            show(page, url)
    if leg_market:
        show(page, f"https://kalshi.com/markets_by_ticker/{leg_market}")

    section("The /combos page: card links, API responses, and where a tap goes")
    api.clear()
    show(page, "https://kalshi.com/combos")
    try:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        print("anchors:", len(hrefs))
        for h in sorted(set(hrefs)):
            if any(k in h.lower() for k in ("combo", "market", "mve", "share", "/t/")):
                print("  ", h)
    except Exception as exc:
        print("anchors ERR", repr(exc))
    for r in api[:12]:
        try:
            body = r.text()
            print(f"  API {r.status} {r.url[:200]}\n     {body[:1500]}")
        except Exception as exc:
            print(f"  API {r.url[:200]} ERR {exc!r}")
    try:
        cards = page.locator("text=/pays \\$/")
        print("cards matching 'pays $':", cards.count())
        if cards.count():
            before = page.url
            cards.first.click(timeout=10000)
            page.wait_for_timeout(5000)
            print("after tapping the first card:", before, "->", page.url)
            print("   text", page.inner_text("body")[:900].replace("\n", " | "))
            share = page.locator("text=/^Share/i")
            print("share buttons:", share.count())
    except Exception as exc:
        print("tap ERR", repr(exc)[:300])
    browser.close()

section("Context around combo / share / ticker routes in the site's JS")
needles = ["markets_by_ticker", "/combos/${", "markets/kxmve", '"/share/', "/share/${", '"/t/', "app.kalshi.com",
           "onelink", "app.link", "deep_link", "deeplink", "selected_legs", "legs=", "combo_ticker", "comboTicker",
           "mveTicker", "mve_ticker"]
seen = {n: 0 for n in needles}
for u in sorted(js_urls):
    try:
        body = requests.get(u, timeout=20, headers={"User-Agent": UA}).text
    except Exception:
        continue
    name = u.rsplit("/", 1)[-1]
    for n in needles:
        start = 0
        while seen[n] < 4:
            i = body.find(n, start)
            if i < 0:
                break
            seen[n] += 1
            print(f"-- {n} in {name}\n   {body[max(0, i - 350):i + 350]!r}")
            start = i + len(n)
print("matches per needle:", seen)
