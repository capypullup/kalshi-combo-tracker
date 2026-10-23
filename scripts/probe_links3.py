"""One-off probe, round 3: does kalshi.com/combos?marketTicker=<combo> load that combo's bet slip? Runs in CI only.

kalshi.com/markets_by_ticker/<combo> (a path the Kalshi app claims) redirects there on the web.
Screenshots go to the output branch next to probe.txt.
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
OUT = Path("/tmp/probe")
OUT.mkdir(exist_ok=True)


def section(title):
    print(f"\n========== {title} ==========", flush=True)


section("Combos from the API")
client = KalshiClient()
combos = []
try:
    page = client._get("/markets/trades", {"limit": 1000})
    seen = []
    for t in page.get("trades", []):
        if t["ticker"].startswith(COMBO_PREFIX) and t["ticker"] not in seen:
            seen.append(t["ticker"])
    for tk in seen[:2]:
        m = client._get(f"/markets/{tk}")["market"]
        names = []
        for leg in m.get("mve_selected_legs") or []:
            lm = client._get(f"/markets/{leg['market_ticker']}")["market"]
            names.append(lm.get("yes_sub_title") or lm.get("title"))
        combos.append({"ticker": tk, "legs": names, "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title")})
        print(json.dumps(combos[-1])[:1200])
except Exception:
    traceback.print_exc(file=sys.stdout)

from playwright.sync_api import sync_playwright  # noqa: E402


def settle(page):
    for _ in range(15):  # the Vercel checkpoint reloads itself once it passes
        title = page.title() or ""
        if "Checkpoint" not in title and "Security" not in title:
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(8000)


js_urls = set()
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    for label, kw in [("phone", dict(user_agent=UA, viewport={"width": 390, "height": 844}, is_mobile=True,
                                     has_touch=True, device_scale_factor=2)),
                      ("desktop", dict(viewport={"width": 1366, "height": 900}))]:
        ctx = browser.new_context(locale="en-US", **kw)
        page = ctx.new_page()
        page.on("response", lambda r: js_urls.add(r.url) if r.url.endswith(".js") else None)
        for i, c in enumerate(combos):
            for url in [f"https://kalshi.com/markets_by_ticker/{c['ticker']}"]:
                section(f"{label}: combo {i} via {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    settle(page)
                    print("landed on", page.url)
                    body = page.inner_text("body")
                    hits = [n for n in c["legs"] if n and n.split(" ")[0] in body]
                    print(f"leg names found on the page: {len(hits)}/{len(c['legs'])} {hits[:6]}")
                    for kwd in ["Combo builder", "Your combo", "Buy", "pays", "Place", "Sign up", "Log in", "legs", "Review"]:
                        print(f"  {kwd!r} occurs {body.count(kwd)}x")
                    print("body text:", body[:2500].replace("\n", " | "))
                    shot = OUT / f"{label}-combo{i}.png"
                    page.screenshot(path=str(shot), full_page=False)
                    print("screenshot", shot.name)
                except Exception as exc:
                    print("ERR", repr(exc)[:400])
        ctx.close()
    browser.close()

section("Context around marketTicker / op_ params in the site's JS")
needles = ["marketTicker", "op_market_ticker", "op_side", '"op_', "op_count", "op_amount", "op_price"]
count = {n: 0 for n in needles}
for u in sorted(js_urls):
    try:
        body = requests.get(u, timeout=20, headers={"User-Agent": UA}).text
    except Exception:
        continue
    name = u.rsplit("/", 1)[-1]
    for n in needles:
        start = 0
        while count[n] < 5:
            i = body.find(n, start)
            if i < 0:
                break
            ctx_txt = body[max(0, i - 300):i + 300]
            start = i + len(n)
            if n == "marketTicker" and "searchParams" not in ctx_txt and "get(" not in ctx_txt and "query" not in ctx_txt.lower():
                continue
            count[n] += 1
            print(f"-- {n} in {name}\n   {ctx_txt!r}")
print("matches per needle:", count)
