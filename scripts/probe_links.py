"""One-off probe: how can a combo be opened in the Kalshi app or website? Runs in CI only.

1. Which kalshi.com paths open the iOS / Android app (Apple's cached AASA, assetlinks).
2. What Kalshi's API says about a live combo market (event, series, collection).
3. Which kalshi.com URLs show that combo, loaded in a real browser, plus combo routes in the site's JS.
"""

import json
import re
import sys
import time
import traceback
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from combo_tracker.client import COMBO_PREFIX, KalshiClient  # noqa: E402

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.5 Mobile/15E148 Safari/604.1")


def section(title):
    print(f"\n========== {title} ==========", flush=True)


def get(url, **kw):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA}, allow_redirects=False, **kw)
        print(f"{r.status_code} {r.headers.get('content-type')} {len(r.content)}B location={r.headers.get('location')}")
        return r
    except Exception as exc:
        print("ERR", repr(exc))
        return None


section("Apple app-site-association (Apple CDN)")
for host in ["kalshi.com", "www.kalshi.com", "link.kalshi.com", "app.kalshi.com", "kalshi.app.link"]:
    print("--", host)
    r = get(f"https://app-site-association.cdn-apple.com/a/v1/{host}")
    if r is not None and r.ok:
        print(r.text[:6000])

section("Direct association files")
for url in ["https://kalshi.com/.well-known/apple-app-site-association", "https://kalshi.com/apple-app-site-association",
            "https://kalshi.com/.well-known/assetlinks.json", "https://kalshi.com/robots.txt"]:
    print("--", url)
    r = get(url)
    if r is not None:
        print(r.text[:3000])

section("A live combo from the API")
client = KalshiClient()
combo = None
try:
    page = client._get("/markets/trades", {"limit": 1000})
    tickers = [t["ticker"] for t in page.get("trades", []) if t["ticker"].startswith(COMBO_PREFIX)]
    print(f"{len(tickers)} combo fills on the first page")
    if tickers:
        combo = client._get(f"/markets/{tickers[0]}")["market"]
        print(json.dumps({k: v for k, v in combo.items() if k not in ("rules_primary", "rules_secondary")}, indent=1)[:5000])
        ev = client._get(f"/events/{combo['event_ticker']}")
        print("event:", json.dumps(ev.get("event", ev), indent=1)[:2500])
        coll = combo.get("mve_collection_ticker")
        if coll:
            c = client._get(f"/multivariate_event_collections/{coll}")
            print("collection:", json.dumps(c, indent=1)[:3000])
        legs = combo.get("mve_selected_legs") or []
        if legs:
            ev0 = client._get(f"/events/{legs[0]['event_ticker']}")
            e = ev0.get("event", ev0)
            print("first leg event:", {k: e.get(k) for k in ("event_ticker", "series_ticker", "title", "sub_title")})
except Exception:
    traceback.print_exc(file=sys.stdout)

section("kalshi.com in a real browser")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright not installed")
    sys.exit(0)

cands = ["https://kalshi.com/", "https://kalshi.com/combos", "https://kalshi.com/sports", "https://kalshi.com/live"]
if combo:
    t, e = combo["ticker"], combo["event_ticker"]
    s = combo.get("series_ticker") or e.split("-")[0]
    legs = combo.get("mve_selected_legs") or []
    cands += [f"https://kalshi.com/markets/{t}", f"https://kalshi.com/markets/{t.lower()}",
              f"https://kalshi.com/markets/{s.lower()}/{e.lower()}", f"https://kalshi.com/markets/{s.lower()}/x/{e.lower()}",
              f"https://kalshi.com/events/{e}", f"https://kalshi.com/combos/{t}", f"https://kalshi.com/combo/{t}"]
    if legs:
        le = legs[0]["event_ticker"]
        cands += [f"https://kalshi.com/markets/{le.split('-')[0].lower()}/{le.lower()}", f"https://kalshi.com/events/{le}"]

js_urls = set()
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(user_agent=UA, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="en-US")
    page = ctx.new_page()
    page.on("response", lambda r: js_urls.add(r.url) if r.url.endswith(".js") or "/_next/static/" in r.url else None)
    for url in cands:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else None
            for _ in range(15):  # the Vercel checkpoint reloads itself once it passes
                if "Checkpoint" not in (page.title() or "") and "Security" not in (page.title() or ""):
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(4000)
            text = page.inner_text("body")[:600].replace("\n", " | ")
            print(f"-- {url}\n   status {status} -> {page.url}\n   title {page.title()!r}\n   text {text}")
        except Exception as exc:
            print(f"-- {url}\n   ERR {exc!r}"[:400])
    browser.close()

section("Combo routes in the site's JS")
pat = re.compile(r'["\'`](/[a-z0-9\-/\[\]\$\{\}:._]*(?:combo|parlay|mve|rfq|share|deeplink|deep-link)[a-z0-9\-/\[\]\$\{\}:._?=&]*)["\'`]', re.I)
schemes = re.compile(r'(kalshi://[a-zA-Z0-9/_\-?=&.]*)')
found, sch = {}, set()
for u in sorted(js_urls):
    if not u.endswith(".js"):
        continue
    try:
        body = requests.get(u, timeout=20, headers={"User-Agent": UA}).text
    except Exception:
        continue
    for m in pat.finditer(body):
        found.setdefault(m.group(1), u.rsplit("/", 1)[-1])
    sch.update(schemes.findall(body))
print(f"{len(js_urls)} scripts seen")
for k, v in sorted(found.items())[:200]:
    print(f"  {k}   ({v})")
print("schemes:", sorted(sch)[:50])
