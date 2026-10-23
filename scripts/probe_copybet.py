"""Throwaway probe (CI only): why doesn't "Bet in Kalshi" put the combo in the Kalshi app's slip?

1. Which kalshi.com paths the iOS / Android apps claim (Apple's AASA, assetlinks).
2. A live combo, one of its legs and that leg's event, from the public API.
3. kalshi.com in a phone browser: where each candidate link lands, whether the combo's slip
   shows, the page's app-banner tags, and the link Kalshi's own Share button makes.
4. The JS the combos page loads, saved for reading offline.
Output: /tmp/out (probe.txt, screenshots, js.tar.gz, facts.json for the Android job).
"""

import json
import sys
import tarfile
import traceback
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from combo_tracker.client import COMBO_PREFIX, KalshiClient  # noqa: E402

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.5 Mobile/15E148 Safari/604.1")
OUT = Path("/tmp/out")
(OUT / "js").mkdir(parents=True, exist_ok=True)
facts = {"packages": []}


def section(title):
    print(f"\n========== {title} ==========", flush=True)


def get(url, **kw):
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA}, allow_redirects=False, **kw)
        print(f"{r.status_code} {r.headers.get('content-type')} {len(r.content)}B location={r.headers.get('location')}")
        return r
    except Exception as exc:
        print("ERR", repr(exc))
        return None


section("Apple app-site-association")
for url in ["https://app-site-association.cdn-apple.com/a/v1/kalshi.com",
            "https://app-site-association.cdn-apple.com/a/v1/www.kalshi.com",
            "https://kalshi.com/.well-known/apple-app-site-association"]:
    print("--", url)
    r = get(url)
    if r is not None and r.ok:
        print(r.text[:20000])

section("Android assetlinks")
for url in ["https://kalshi.com/.well-known/assetlinks.json"]:
    print("--", url)
    r = get(url)
    if r is not None and r.ok:
        print(r.text[:5000])
for site in ["https://kalshi.com", "https://www.kalshi.com"]:
    try:
        r = requests.get("https://digitalassetlinks.googleapis.com/v1/statements:list",
                         params={"source.web.site": site, "relation": "delegate_permission/common.handle_all_urls"},
                         timeout=20)
        print("-- DAL", site, r.status_code, r.text[:2000])
        for s in r.json().get("statements", []):
            pkg = ((s.get("target") or {}).get("androidApp") or {}).get("packageName")
            if pkg and pkg not in facts["packages"]:
                facts["packages"].append(pkg)
    except Exception as exc:
        print("-- DAL", site, "ERR", repr(exc))
print("android packages:", facts["packages"])

section("A live combo, a leg and its event")
client = KalshiClient()
combo = None
try:
    page = client._get("/markets/trades", {"limit": 1000})
    tickers = []
    for t in page.get("trades", []):
        if t["ticker"].startswith(COMBO_PREFIX) and t["ticker"] not in tickers:
            tickers.append(t["ticker"])
    print(f"{len(tickers)} combo tickers on the first trades page")
    for tk in tickers[:40]:  # prefer a short, open combo, easier to read in a screenshot
        try:
            m = client._get(f"/markets/{tk}")["market"]
        except Exception as exc:
            print("skip", tk, repr(exc)[:120])
            continue
        if m.get("status") == "active" and 2 <= len(m.get("mve_selected_legs") or []) <= 4:
            combo = m
            break
        combo = combo or (m if m.get("status") == "active" else None)
    if combo is None:  # fall back to the markets list
        page = client._get("/markets", {"mve_filter": "only", "status": "open", "limit": 50})
        combo = next(m for m in page.get("markets", []) if m.get("mve_selected_legs"))
    legs = combo.get("mve_selected_legs") or []
    names = []
    for leg in legs:
        lm = client._get(f"/markets/{leg['market_ticker']}")["market"]
        names.append(lm.get("yes_sub_title") or lm.get("title") or "")
    facts.update(combo=combo["ticker"], combo_event=combo["event_ticker"], legs=legs, leg_names=names,
                 collection=combo.get("mve_collection_ticker"),
                 leg=legs[0]["market_ticker"] if legs else None, leg_event=legs[0]["event_ticker"] if legs else None)
    ev = client._get(f"/events/{facts['leg_event']}")
    e = ev.get("event", ev)
    facts["leg_series"] = e.get("series_ticker")
    print(json.dumps(facts, indent=1))
except Exception:
    traceback.print_exc(file=sys.stdout)
(OUT / "facts.json").write_text(json.dumps(facts, indent=1))

from playwright.sync_api import sync_playwright  # noqa: E402

# Record what the page shares or copies, so a tap on Kalshi's Share button shows its link.
HOOK = """
window.__cap = [];
const rec = (kind, v) => { try { window.__cap.push([kind, JSON.stringify(v)]); } catch (e) {} };
try { navigator.share = (d) => { rec('share', d); return Promise.resolve(); }; } catch (e) {}
try { navigator.canShare = () => true; } catch (e) {}
try {
  if (!navigator.clipboard) Object.defineProperty(navigator, 'clipboard', {value: {}});
  navigator.clipboard.writeText = (t) => { rec('clipboard', t); return Promise.resolve(); };
  navigator.clipboard.write = (items) => { rec('clipboard-items', String(items)); return Promise.resolve(); };
} catch (e) {}
const ex = document.execCommand.bind(document);
document.execCommand = (c, ...a) => { if (c === 'copy') rec('execCommand', String(window.getSelection())); return ex(c, ...a); };
const wo = window.open;
window.open = (u, ...a) => { rec('window.open', String(u)); return null; };
"""


def settle(page, extra=6000):
    for _ in range(20):  # the Vercel checkpoint reloads itself once it passes
        title = page.title() or ""
        if "Checkpoint" not in title and "Security" not in title and "verify" not in title.lower():
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(extra)


def meta_tags(page):
    return page.eval_on_selector_all(
        "meta, link[rel=alternate], link[rel=manifest]",
        "els => els.map(e => e.outerHTML).filter(h => /app|itunes|al:|twitter|android|ios|apple|og:url/i.test(h))")


def anchors(page):
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    return sorted({h for h in hrefs if h and not h.startswith("#")})


def buttons(page):
    return page.eval_on_selector_all(
        "button, [role=button]",
        "els => els.map(e => ((e.getAttribute('aria-label') || '') + ' | ' + (e.innerText || '').trim()).slice(0, 80))")


def visit(page, url, label, shots, share=False):
    section(f"{label}: {url}")
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        settle(page)
        print("status", resp.status if resp else None, "landed on", page.url)
        print("title", repr(page.title()))
        body = page.inner_text("body")
        names = facts.get("leg_names") or []
        hits = [n for n in names if n and n[:12] in body]
        print(f"leg names on the page: {len(hits)}/{len(names)} {hits}")
        for k in ["Combo", "combo", "Buy", "Yes", "No", "pays", "Request", "quote", "Quote", "Log in", "Sign up",
                  "Open in app", "Open app", "Get the app", "Download"]:
            print(f"  {k!r} x{body.count(k)}")
        print("meta:", json.dumps(meta_tags(page), indent=0)[:3000])
        a = anchors(page)
        print(f"anchors ({len(a)}):", [h for h in a if any(w in h.lower() for w in
                                                           ("app", "share", "combo", "market", "onelink", "/t/",
                                                            "itunes", "apple", "play.google", "intent:"))][:80])
        print("buttons:", buttons(page)[:80])
        print("body:", body[:2500].replace("\n", " | "))
        shot = OUT / f"{label}.png"
        page.screenshot(path=str(shot))
        shots.append(shot.name)
        if share:
            for sel in ["[aria-label*='hare' i]", "button:has-text('Share')", "[data-testid*='share' i]",
                        "svg[aria-label*='share' i]"]:
                loc = page.locator(sel)
                n = loc.count()
                print(f"share candidates {sel}: {n}")
                for i in range(min(n, 3)):
                    try:
                        loc.nth(i).click(timeout=5000)
                        page.wait_for_timeout(2500)
                        cap = page.evaluate("window.__cap || []")
                        print(f"   after click {i}: captured {cap}")
                        page.screenshot(path=str(OUT / f"{label}-share{i}.png"))
                        shots.append(f"{label}-share{i}.png")
                        # Any dialog with a link or a "Copy link" button?
                        for txt in ["Copy link", "Copy Link", "Copy"]:
                            c = page.locator(f"text={txt}")
                            if c.count():
                                c.first.click(timeout=4000)
                                page.wait_for_timeout(1500)
                                print(f"   after '{txt}': captured {page.evaluate('window.__cap || []')}")
                                break
                        page.keyboard.press("Escape")
                    except Exception as exc:
                        print("   click ERR", repr(exc)[:200])
    except Exception as exc:
        print("ERR", repr(exc)[:500])


js_urls, api_calls, shots = set(), [], []
with sync_playwright() as p:
    for engine, kw in [
        ("chromium", dict(user_agent=UA, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                          device_scale_factor=2)),
    ]:
        try:
            browser = getattr(p, engine).launch(**({"args": ["--disable-blink-features=AutomationControlled"]}
                                                   if engine == "chromium" else {}))
        except Exception as exc:
            print(engine, "launch ERR", repr(exc)[:300])
            continue
        ctx = browser.new_context(locale="en-US", **kw)
        ctx.add_init_script(HOOK)
        page = ctx.new_page()
        page.on("response", lambda r: js_urls.add(r.url) if r.url.split("?")[0].endswith(".js") else None)
        page.on("request", lambda r: api_calls.append(f"{r.method} {r.url[:220]}")
                if ("api" in r.url and "kalshi" in r.url) else None)
        if facts.get("combo"):
            t = facts["combo"]
            visit(page, f"https://kalshi.com/combos?marketTicker={t}", f"{engine}-combos-param", shots, share=True)
            try:  # open the slip's bottom sheet and read it
                sheet = page.locator("text=/MARKETS? COMBO/i").first
                sheet.click(timeout=8000)
                page.wait_for_timeout(3000)
                body = page.inner_text("body")
                names = facts.get("leg_names") or []
                print("after opening the slip, leg names found:",
                      [n for n in names if n and n.split(":")[0] in body], "of", names)
                i = body.find("COMBO")
                print("slip text:", body[max(0, i - 200):i + 1500].replace("\n", " | "))
                print("slip buttons:", buttons(page)[:30])
                page.screenshot(path=str(OUT / f"{engine}-slip-open.png"))
                shots.append(f"{engine}-slip-open.png")
            except Exception as exc:
                print("slip ERR", repr(exc)[:300])
            print("API calls while loading the combo slip:", "\n  ".join([""] + api_calls[-60:]))
            visit(page, f"https://kalshi.com/markets_by_ticker/{t}", f"{engine}-mbt-combo", shots)
        if facts.get("leg"):
            visit(page, f"https://kalshi.com/markets_by_ticker/{facts['leg']}", f"{engine}-mbt-leg", shots, share=True)
        if engine == "chromium":
            visit(page, "https://kalshi.com/combos", f"{engine}-combos-list", shots)
            try:
                card = page.locator("button:has-text('pays $')").first
                card.click(timeout=8000)
                page.wait_for_timeout(5000)
                print("after tapping the first popular combo:", page.url)
                print("   body:", page.inner_text("body")[:1500].replace("\n", " | "))
                print("   buttons:", buttons(page)[:40])
                page.screenshot(path=str(OUT / "chromium-popular-combo.png"))
                shots.append("chromium-popular-combo.png")
                for sel in ["[aria-label*='hare' i]", "button:has-text('Share')"]:
                    loc = page.locator(sel)
                    print("share candidates", sel, loc.count())
                    if loc.count():
                        loc.first.click(timeout=5000)
                        page.wait_for_timeout(2500)
                        print("   captured", page.evaluate("window.__cap || []"))
            except Exception as exc:
                print("popular combo ERR", repr(exc)[:300])
        ctx.close()
        browser.close()

section("Saving the JS the pages loaded")
saved = 0
with tarfile.open(OUT / "js.tar.gz", "w:gz") as tar:
    for u in sorted(js_urls):
        try:
            body = requests.get(u, timeout=25, headers={"User-Agent": UA}).content
        except Exception:
            continue
        name = u.split("?")[0].replace("https://", "").replace("/", "__")[-180:]
        f = OUT / "js" / name
        f.write_bytes(body)
        tar.add(f, arcname=name)
        saved += 1
print(f"saved {saved} of {len(js_urls)} JS files")
(OUT / "facts.json").write_text(json.dumps(facts, indent=1))
