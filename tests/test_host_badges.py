"""Streamlit Cloud's badges sit over the app's bottom right; app.py's HOST_BADGES_JS makes room for them."""

import ast
import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

APP = Path(__file__).parents[1] / "app.py"
CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
sync_api = pytest.importorskip("playwright.sync_api")


def app_constant(name: str) -> str:
    for node in ast.parse(APP.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise LookupError(name)


# Like Streamlit Cloud (measured on a real app page at 390x844): the app in a full-screen frame, the
# Streamlit badge (66x46) in the bottom right corner and the creator's avatar (80x46) next to it.
HOST = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0}iframe{position:fixed;inset:0;width:100%;height:100%;border:0}
.badge{position:fixed;right:0;bottom:0;width:66px;height:46px;background:red}
.avatar{position:fixed;right:57px;bottom:0;width:80px;height:46px}</style>
<iframe src="app.html"></iframe><a class="badge" href="#logo"></a><div class="avatar"><a href="#me">me</a></div>__MORE__"""
# The owner also gets a wide "Manage app" button: too wide to keep five tabs beside, so the tabs go above.
MANAGE = '<button style="position:fixed;right:142px;bottom:6px;width:110px;height:34px">Manage app</button>'
# The tab bar's padding rule from app.py, with five tabs.
APP_PAGE = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0}.bar{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;
padding:6px max(6px, calc(50vw - 380px), var(--host-right, 0px)) calc(6px + var(--host-lift, 0px)) max(6px, calc(50vw - 380px))}
.bar a{padding:10px 0}</style>
<div class="bar">""" + "".join(f'<a href="#{t}" id="{t}">{t}</a>' for t in ("combos", "bettors", "home", "quote", "tools")) + "</div>"


@pytest.fixture
def site(tmp_path):
    (tmp_path / "host.html").write_text(HOST.replace("__MORE__", ""))
    (tmp_path / "owner.html").write_text(HOST.replace("__MORE__", MANAGE))
    (tmp_path / "app.html").write_text(APP_PAGE + app_constant("HOST_BADGES_JS"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.log_message = lambda *a: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def page():
    try:
        with sync_api.sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM)
            yield browser.new_page(viewport={"width": 390, "height": 844})
            browser.close()
    except sync_api.Error as exc:  # no browser installed
        pytest.skip(str(exc)[:100])


def host_vars(page, host: str) -> tuple:
    page.goto(host)
    frame = page.frames[1]
    frame.wait_for_function("getComputedStyle(document.documentElement).getPropertyValue('--host-lift') !== ''")
    px = lambda name: int(frame.evaluate(f"getComputedStyle(document.documentElement).getPropertyValue('{name}')")[:-2])
    return frame, px("--host-right"), px("--host-lift")


def tappable(page, frame) -> dict:
    """Whether a tap on each tab's center reaches the app's frame rather than a badge."""
    out = {}
    for tab in ("combos", "bettors", "home", "quote", "tools"):
        box = frame.locator(f"#{tab}").bounding_box()
        top = page.evaluate("([x, y]) => document.elementFromPoint(x, y).tagName",
                            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
        out[tab] = top == "IFRAME"
    return out


def test_tabs_keep_left_of_the_badges(site, page):
    frame, right, lift = host_vars(page, f"{site}/host.html")
    assert (right, lift) == (141, 0)  # the avatar starts 253px in on a 390px screen, plus 4px
    assert all(tappable(page, frame).values())


def test_tabs_go_above_a_wide_manage_button(site, page):
    frame, right, lift = host_vars(page, f"{site}/owner.html")
    assert right == 0 and lift == 50  # the badges are 46px tall, plus 4px
    assert all(tappable(page, frame).values())


def test_nothing_changes_outside_a_host_page(site, page):
    page.goto(f"{site}/app.html")
    page.wait_for_timeout(300)
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--host-right')") == ""
