"""Load CapyEV the way a phone does and time it: first load, then each bottom tab.

    python scripts/phone_probe.py https://capyev.streamlit.app/ --profile slow4g --out probe.json

Profiles throttle Chromium like a phone on a network: `fast` is a recent iPhone on good 5G/Wi-Fi,
`slow4g` is Lighthouse's mid-range Android on slow 4G. It also lists what the host draws over the
app near the bottom (Streamlit Cloud's badges) and whether each tab can be tapped, and can save
screenshots. Needs `pip install playwright` and a Chromium (`playwright install chromium`, or point
CHROMIUM_PATH at one).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
from pathlib import Path

from playwright.sync_api import Frame, Page, sync_playwright

PROFILES = {  # round trip ms, download / upload kbit/s, CPU slowdown
    "none": None,
    "fast": {"rtt": 40, "down": 30_000, "up": 10_000, "cpu": 1.5},
    "slow4g": {"rtt": 150, "down": 1_638, "up": 750, "cpu": 4},
}
IPHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True, "has_touch": True,
          "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"}
# What each page shows once it has drawn (the tab bar lights the page's tab as it renders).
READY = {
    "home": ".home .card, .home .empty",
    "combos": ".cev .row, .cev .empty",
    "bettors": ".cev .row, .cev .empty, .cev",
    "quote": '[data-testid="stSelectbox"]',
    "tools": '[data-testid="stNumberInput"] input',
}
ON = {"home": ".st-key-capy_on", "combos": ".st-key-tab_on a[href$='combos']",
      "bettors": ".st-key-tab_on a[href$='bettors']", "quote": ".st-key-tab_on a[href$='quote']",
      "tools": ".st-key-tab_on a[href$='tools']"}

# Fixed or absolute elements of the top page near the bottom: what the host draws over the app.
OVERLAYS_JS = """() => {
  const out = [], H = innerHeight;
  for (const el of document.querySelectorAll('body *')) {
    if (el.tagName === 'IFRAME' || el.closest('iframe')) continue;
    const cs = getComputedStyle(el);
    if (!['fixed', 'absolute', 'sticky'].includes(cs.position)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    if (r.bottom < H - 300) continue;
    out.push({tag: el.tagName.toLowerCase(), id: el.id, cls: String(el.className && (el.className.baseVal ?? el.className)),
              testid: el.dataset.testid || null, pos: cs.position, z: cs.zIndex,
              rect: [r.x, r.y, r.width, r.height].map(Math.round), html: el.outerHTML.slice(0, 700)});
  }
  return out;
}"""

# The top page's markup without the app frame, to see how the host builds its badges.
SHELL_JS = """() => {
  const c = document.body.cloneNode(true);
  c.querySelectorAll('script, style, svg path').forEach((e) => e.remove());
  c.querySelectorAll('iframe').forEach((e) => { e.removeAttribute('srcdoc'); });
  return c.outerHTML.slice(0, 20000);
}"""


# Which strips along the bottom of the screen a tap would land on the host's page instead of the app.
COVERED_JS = """() => {
  const W = innerWidth, H = innerHeight, rows = {};
  for (const dy of [12, 30, 50, 75, 100, 130]) {
    const y = H - dy, runs = [];
    let start = null, what = null;
    for (let x = 2; x <= W; x += 4) {
      const e = x < W ? document.elementFromPoint(x, y) : null;
      const host = e && e.tagName !== 'IFRAME' && e !== document.body && e !== document.documentElement;
      const label = host ? (e.closest('a,button,[data-testid]') || e).outerHTML.slice(0, 90) : null;
      if (host && start === null) { start = x; what = label; }
      if (!host && start !== null) { runs.push({x: [start, x - 4], what}); start = null; }
    }
    if (runs.length) rows[dy] = runs;
  }
  return rows;
}"""


class Wire:
    """Bytes the page downloads, from Chrome's own network events."""

    def __init__(self, cdp):
        self.bytes = 0
        self.requests = 0
        self.ws_in = 0
        self.ws_frames = 0
        self.ws_big = 0
        if cdp is None:  # WebKit has no Chrome DevTools: no byte counts
            return
        cdp.on("Network.loadingFinished", self._done)
        cdp.on("Network.webSocketFrameReceived", self._frame)

    def _done(self, e):
        self.bytes += e.get("encodedDataLength", 0)
        self.requests += 1

    def _frame(self, e):
        n = len(e["response"].get("payloadData", ""))
        n = n * 3 // 4 if e["response"].get("opcode") == 2 else n  # binary frames arrive base64-encoded
        self.ws_in += n
        self.ws_frames += 1
        self.ws_big = max(self.ws_big, n)

    def mark(self) -> dict:
        return {"bytes": self.bytes, "requests": self.requests, "ws_in": self.ws_in, "ws_frames": self.ws_frames}

    def since(self, m: dict) -> dict:
        return {k: v - m[k] for k, v in self.mark().items()}


def throttle(cdp, profile: str) -> None:
    p = PROFILES[profile]
    if not p:
        return
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": p["rtt"], "downloadThroughput": p["down"] * 1000 / 8,
        "uploadThroughput": p["up"] * 1000 / 8})
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": p["cpu"]})


def app_frame(page: Page, timeout: float = 120) -> Frame | None:
    """The frame the app runs in: Streamlit Cloud puts it in an iframe at /~/+/; elsewhere it's the page."""
    end = time.time() + timeout
    while time.time() < end:
        if "/errors/" in page.url:  # Streamlit Cloud's "no access or doesn't exist" page
            return None
        for f in page.frames:
            if f is not page.main_frame and "/~/+/" in f.url:
                return f
        try:
            if page.main_frame.query_selector(".st-key-tabbar, [data-testid='stApp']"):
                return page.main_frame
        except Exception:
            pass
        time.sleep(0.05)
    return None


def wake_if_asleep(page: Page) -> float | None:
    """Streamlit Cloud shows a sleep page after a while without visitors; press its wake button."""
    try:
        btn = page.get_by_role("button", name="Yes, get this app back up!")
        if btn.is_visible(timeout=3000):
            t = time.time()
            btn.click()
            return t
    except Exception:
        pass
    return None


def shot(page: Page, name: str, out_dir: Path | None, show: bool) -> None:
    img = page.screenshot(type="jpeg", quality=40, scale="css")
    if out_dir:
        (out_dir / f"{name}.jpg").write_bytes(img)
    if show:  # CI logs: decode the block between the markers to see it
        print(f"=====SHOT {name} {len(img)}B=====\n{base64.b64encode(img).decode()}\n=====END SHOT=====")


def blocked_tabs(page: Page, frame: Frame) -> list[dict]:
    """For each tab, what the top page has at its center: the app's frame (tappable) or something on top."""
    if frame is page.main_frame:
        return []
    box = page.query_selector("iframe").bounding_box() if page.query_selector("iframe") else {"x": 0, "y": 0}
    res = []
    for a in frame.query_selector_all(".st-key-tabbar [data-testid='stPageLink'] a"):
        b = a.bounding_box()
        if not b:
            continue
        x, y = box["x"] + b["x"] + b["width"] / 2, box["y"] + b["y"] + b["height"] / 2
        top = page.evaluate("""([x, y]) => { const e = document.elementFromPoint(x, y);
            return e ? {tag: e.tagName.toLowerCase(), cls: String(e.className && (e.className.baseVal ?? e.className)),
                        testid: e.dataset.testid || null, text: (e.innerText || '').slice(0, 40)} : null; }""", [x, y])
        res.append({"tab": a.get_attribute("href"), "center": [round(x), round(y)], "tappable": bool(top and top["tag"] == "iframe"),
                    "top": top})
    return res


def what_shows(page: Page) -> dict:
    """When the app doesn't come up: where the browser ended up and what it shows."""
    out = {"url": page.url, "frames": [f.url for f in page.frames]}
    try:
        out["title"] = page.title()
        out["text"] = page.evaluate("() => document.body ? document.body.innerText.slice(0, 1500) : ''")
        out["buttons"] = page.evaluate("() => [...document.querySelectorAll('button, a')].map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 30)")
    except Exception as exc:
        out["read_error"] = str(exc)[:200]
    for f in page.frames[1:4]:
        try:
            out.setdefault("frame_text", []).append(f.evaluate("() => document.body ? document.body.innerText.slice(0, 600) : ''"))
        except Exception:
            pass
    return out


def first_load(page: Page, wire: Wire, url: str, timeout: float, overlays_only: bool = False) -> dict:
    t0 = time.time()
    page.goto(url, wait_until="commit", timeout=timeout * 1000)
    page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
    out = {"dom_ms": round((time.time() - t0) * 1000)}
    woke = wake_if_asleep(page)
    if woke:
        out["was_asleep"] = True
    frame = app_frame(page, timeout)
    if frame is None:
        out["error"] = "the app never showed up"
        out["page"] = what_shows(page)
        return out
    out["frame_ms"] = round((time.time() - t0) * 1000)
    if overlays_only:  # any Streamlit app: just wait for it to draw
        frame.wait_for_selector("[data-testid='stApp']", state="visible", timeout=timeout * 1000)
        frame.wait_for_selector("[data-testid='stMainBlockContainer'] > div", state="visible", timeout=timeout * 1000)
        out["app_ms"] = round((time.time() - t0) * 1000)
        out["wire"] = wire.mark()
        return out
    frame.wait_for_selector(".st-key-tabbar", state="visible", timeout=timeout * 1000)
    out["tabbar_ms"] = round((time.time() - t0) * 1000)
    frame.wait_for_selector(READY["home"], state="visible", timeout=timeout * 1000)
    out["content_ms"] = round((time.time() - t0) * 1000)
    if woke:
        out["wake_ms"] = round((time.time() - woke) * 1000)
    out["wire"] = wire.mark()
    return out


def switch(page: Page, frame: Frame, wire: Wire, to: str, timeout: float) -> dict:
    link = frame.query_selector(".st-key-tabbar [class*='st-key-capy'] a" if to == "home"
                                else f".st-key-tabbar a[href$='{to}']")
    m = wire.mark()
    t0 = time.time()
    link.click()
    frame.wait_for_selector(ON[to], state="attached", timeout=timeout * 1000)
    lit = time.time()
    frame.wait_for_selector(READY[to], state="visible", timeout=timeout * 1000)
    # Wait for Streamlit to finish the run (stale elements fade while a run is going).
    try:
        frame.wait_for_function("() => !document.querySelector('[data-stale=\"true\"]')", timeout=timeout * 1000)
    except Exception:
        pass
    done = time.time()
    return {"to": to, "lit_ms": round((lit - t0) * 1000), "ready_ms": round((done - t0) * 1000), "wire": wire.since(m)}


def run(url: str, profile: str, rounds: int, out_dir: Path | None, show: bool, timeout: float,
        engine: str = "chromium", switch_timeout: float = 45, overlays_only: bool = False) -> dict:
    res = {"url": url, "profile": profile, "engine": engine}
    with sync_playwright() as pw:
        if engine == "webkit":  # Safari's engine, for how an iPhone lays the page out (no throttling)
            browser = pw.webkit.launch()
            ctx = browser.new_context(**{k: v for k, v in pw.devices["iPhone 13"].items() if k != "default_browser_type"})
            page = ctx.new_page()
            wire = Wire(None)
        else:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            ctx = browser.new_context(**IPHONE)
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            wire = Wire(cdp)
            throttle(cdp, profile)
        try:
            res["first_load"] = first_load(page, wire, url, timeout, overlays_only)
        except Exception as exc:
            res["first_load"] = {"error": str(exc)[:300], "page": what_shows(page)}
        if "error" in res["first_load"]:
            shot(page, f"{engine}-{profile}-failed", out_dir, show)
            browser.close()
            return res
        frame = app_frame(page, 5)
        page.wait_for_timeout(5000 if overlays_only else 2500)  # the host's badges come in after the app
        res["overlays"] = page.evaluate(OVERLAYS_JS)
        res["tabs"] = blocked_tabs(page, frame)
        res["covered_px_from_bottom"] = page.evaluate(COVERED_JS) if frame is not page.main_frame else {}
        res["touch_icon_top"] = page.evaluate(
            "() => (document.querySelector('link[rel=\"apple-touch-icon\"]') || {}).href || null")
        if frame is not page.main_frame:
            res["frame_url"] = frame.url
        shot(page, f"{engine}-{profile}-home", out_dir, show)
        if profile == "none":
            res["shell"] = page.evaluate(SHELL_JS)
        if overlays_only:
            browser.close()
            return res
        print("=====FIRST LOOK=====\n" + json.dumps(res, indent=1, default=str) + "\n=====END FIRST LOOK=====", flush=True)

        res["switches"] = []
        order = ["combos", "home", "quote", "tools", "home", "bettors", "home"]
        for _ in range(rounds):
            for to in order:
                try:
                    res["switches"].append(switch(page, frame, wire, to, switch_timeout))
                except Exception as exc:
                    res["switches"].append({"to": to, "error": str(exc)[:200]})
                    shot(page, f"{profile}-{to}-failed", out_dir, show)
        res["summary"] = {}
        for to in set(order):
            ok = [s for s in res["switches"] if s["to"] == to and "ready_ms" in s]
            if ok:
                res["summary"][to] = {"lit_ms": statistics.median(s["lit_ms"] for s in ok),
                                      "ready_ms": statistics.median(s["ready_ms"] for s in ok),
                                      "ws_in": statistics.median(s["wire"]["ws_in"] for s in ok), "n": len(ok)}
        browser.close()
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--profile", default="slow4g", choices=sorted(PROFILES))
    ap.add_argument("--engine", default="chromium", choices=["chromium", "webkit"])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=240, help="seconds the first load may take (a sleeping app wakes slowly)")
    ap.add_argument("--switch-timeout", type=float, default=45)
    ap.add_argument("--shots", type=Path, help="save screenshots here")
    ap.add_argument("--show", action="store_true", help="print screenshots as base64 (for CI logs)")
    ap.add_argument("--overlays-only", action="store_true",
                    help="any Streamlit app: time the first load and list what the host draws over it, no tabs")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)
    res = run(args.url, args.profile, args.rounds, args.shots, args.show, args.timeout, args.engine, args.switch_timeout,
              args.overlays_only)
    text = json.dumps(res, indent=1, default=str)
    if args.out:
        args.out.write_text(text)
    print("=====RESULT=====")
    print(text)
    print("=====END RESULT=====")
    sys.exit(0 if "error" not in res.get("first_load", {}) else 1)


if __name__ == "__main__":
    main()
