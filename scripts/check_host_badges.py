"""Run app.py's HOST_BADGES_JS inside a real Streamlit Cloud app's frame, with a five-tab bar like CapyEV's,
and report what it sets and whether each tab can be tapped. Usage: python check_host_badges.py URL"""
import ast, base64, json, os, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = sys.argv[1]
js = next(ast.literal_eval(n.value) for n in ast.parse(Path("app.py").read_text()).body
          if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "HOST_BADGES_JS" for t in n.targets))
body = js.split("<script>", 1)[1].rsplit("</script>", 1)[0].strip().rstrip(";")
BAR = r"""() => { const st = document.createElement('style');
  st.textContent = `.capy-bar{position:fixed;left:0;right:0;bottom:0;z-index:999990;display:flex;justify-content:space-around;
  background:#000;padding:6px max(6px, calc(50vw - 380px), var(--host-right, 0px)) calc(6px + var(--host-lift, 0px)) max(6px, calc(50vw - 380px))}
  .capy-bar a{color:#fff;padding:12px 0;font:800 11px sans-serif}`;
  document.head.appendChild(st); const bar = document.createElement('div'); bar.className = 'capy-bar';
  bar.innerHTML = ['combos','bettors','home','quote','tools'].map(t => `<a id="t-${t}">${t}</a>`).join('');
  document.body.appendChild(bar); }"""
for engine, device in (("chromium", "iPhone 13"), ("webkit", "iPhone 13"), ("webkit", "iPhone 15 Pro Max"), ("chromium", "Pixel 7")):
    out = {"engine": engine, "device": device}
    try:
        with sync_playwright() as pw:
            b = getattr(pw, engine).launch(**({"executable_path": os.environ["CHROMIUM_PATH"]} if engine == "chromium" and os.environ.get("CHROMIUM_PATH") else {}))
            ctx = b.new_context(**{k: v for k, v in pw.devices[device].items() if k != "default_browser_type"})
            page = ctx.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            end, frame = time.time() + 120, None
            while time.time() < end and frame is None:
                frame = next((f for f in page.frames if "/~/+/" in f.url), None)
                page.wait_for_timeout(200)
            frame.wait_for_selector("[data-testid='stApp']", timeout=120000)
            page.wait_for_timeout(6000)
            frame.evaluate(BAR)
            frame.evaluate(body)
            page.wait_for_timeout(2000)
            out["vars"] = frame.evaluate("""() => { const s = getComputedStyle(document.documentElement);
                return {right: s.getPropertyValue('--host-right'), lift: s.getPropertyValue('--host-lift'),
                        frameElement: (() => { try { return !!window.frameElement; } catch (e) { return String(e); } })()}; }""")
            fb = page.locator("iframe[title='streamlitApp']").bounding_box()
            out["viewport"] = page.viewport_size
            out["tabs"] = {}
            for t in ("combos", "bettors", "home", "quote", "tools"):
                bb = frame.locator(f"#t-{t}").bounding_box()
                x, y = fb["x"] + bb["x"] + bb["width"] / 2, fb["y"] + bb["y"] + bb["height"] / 2
                out["tabs"][t] = [round(x), round(y), page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); return e ? e.tagName + '.' + String(e.className && (e.className.baseVal ?? e.className)).slice(0, 40) : null; }", [x, y])]
            vh = page.viewport_size["height"]
            img = page.screenshot(type="jpeg", quality=45, scale="css", clip={"x": 0, "y": vh - 120, "width": page.viewport_size["width"], "height": 120})
            print(f"=====SHOT {engine}-{device.replace(' ', '')} {len(img)}B=====\n{base64.b64encode(img).decode()}\n=====END SHOT=====")
            b.close()
    except Exception as exc:
        out["error"] = str(exc)[:300]
    print("=====CHECK=====\n" + json.dumps(out) + "\n=====END CHECK=====", flush=True)
