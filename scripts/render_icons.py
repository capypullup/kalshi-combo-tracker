"""Render the CapyEV logo (combo_tracker/ui/capyev.svg) into the app's PNG icons.

Needs Playwright with Chromium (a dev tool, not an app requirement):
    python scripts/render_icons.py

- combo_tracker/ui/capyev-icon.png: the browser-tab icon (st.set_page_config page_icon)
- static/capyev-touch.png: the home-screen icon, on the app's dark background
"""

import glob
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
LOGO = (ROOT / "combo_tracker" / "ui" / "capyev.svg").read_text()
INNER = LOGO.split(">", 1)[1].rsplit("</svg>", 1)[0]

TOUCH = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <radialGradient id="bg" cx="50%" cy="38%" r="70%"><stop offset="0" stop-color="#0b3a46"/><stop offset="1" stop-color="#010607"/></radialGradient>
  <rect width="100" height="100" fill="url(#bg)"/>
  <g transform="translate(13 12) scale(.74)">{INNER}</g>
</svg>"""


def render(svg: str, size: int, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    sized = svg.replace("<svg ", '<svg width="100%" height="100%" ', 1)
    page.set_content(f'<body style="margin:0"><div style="width:{size}px;height:{size}px">{sized}</div></body>')
    page.locator("div").first.screenshot(path=str(out), omit_background=True)
    print(f"wrote {out.relative_to(ROOT)} ({size}x{size})")


with sync_playwright() as p:
    found = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    browser = p.chromium.launch(executable_path=found[0] if found else None)
    page = browser.new_page(viewport={"width": 400, "height": 400})
    render(LOGO, 128, ROOT / "combo_tracker" / "ui" / "capyev-icon.png")
    render(TOUCH, 180, ROOT / "static" / "capyev-touch.png")
    browser.close()
