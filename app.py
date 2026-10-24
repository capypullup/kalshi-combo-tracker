"""CapyEV: a phone-first Streamlit dashboard for Kalshi combo bets and their EV.

Run locally:  streamlit run app.py
Offline demo: DEMO=1 streamlit run app.py

The table itself is a small custom component (combo_tracker/ui). Filters, sorting,
expanding a bet and copying its card all run in the browser. Scoring runs in a
background worker; the page only takes its latest results when the min bet or
window changes and on refresh (which asks the worker for a fresh poll and score
first), so the list never changes while you read it.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import streamlit as st


def _reload_if_deployed() -> None:
    """Streamlit Cloud pulls a new commit and reruns this file without re-importing
    combo_tracker, so new app code would run against old modules (and old cached
    rows). When the package's files differ from what was imported, start it fresh."""
    pkg = sys.modules.get("combo_tracker")
    if pkg is None:
        return
    root = Path(pkg.__file__).parent
    on_disk = {str(p.relative_to(root)): (p.stat().st_mtime, p.stat().st_size) for p in sorted(root.rglob("*.py"))}
    if getattr(pkg, "LOADED_FILES", None) == on_disk:
        return
    for name in [n for n in sys.modules if n == "combo_tracker" or n.startswith("combo_tracker.")]:
        del sys.modules[name]
    st.cache_resource.clear()  # get_source() then stops the old collector and builds a new one
    st.cache_data.clear()


_reload_if_deployed()

from combo_tracker import activity  # noqa: E402  (after the reload check)
from combo_tracker import financial  # noqa: E402
from combo_tracker.bettors import BettorTracker  # noqa: E402
from combo_tracker.client import KalshiAuth, KalshiClient  # noqa: E402
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.demo import DemoClient, demo_rfq_book, demo_store  # noqa: E402
from combo_tracker.home import pack_home  # noqa: E402
from combo_tracker.keys import find_secret, kalshi_auth  # noqa: E402
from combo_tracker.pipeline import Board, Scorer, TradesUnavailable  # noqa: E402
from combo_tracker import quote_view  # noqa: E402
from combo_tracker import tools_view  # noqa: E402
from combo_tracker import recorder  # noqa: E402
from combo_tracker.recorder import build_view, pack_live, pack_view  # noqa: E402
from combo_tracker.view import LIVE_HOURS, num, pack, pack_bettors, slim_legs  # noqa: E402

DEMO = os.environ.get("DEMO") == "1"
SCORE_SECONDS = 120  # how often the background worker re-scores the views people have open
DEPTH_SECONDS = 60  # how long a "full book" lookup is cached
MAX_BETS = 600  # newest bets scored per view
MIN_BETS = (5, 10, 50, 100, 500)  # the Min bet choices on Combos (MIN_BET in ui/table.js)
DEFAULT_VIEW = (60, 50)  # Combos opens on the last hour, bets of $50+
HOME_VIEW = (60, 100)  # last hour, bets of $100+: a view the worker always keeps scored
MAX_WINDOW = 180  # minutes of combo fills the collector keeps
DEEP_TTL = 180  # seconds a "full book" result stays on an expanded bet
REFRESH_WAIT = 45  # seconds the refresh button waits for new scores before saying Kalshi is slow
STALE_SECONDS = 600  # a page opened on scores older than this (the app sat idle) brews fresh ones
TRADES_WAIT = 60  # seconds a page waits for the collector's first poll after start-up or an idle pause

UI = Path(__file__).parent / "combo_tracker" / "ui"
LOGO = (UI / "capyev.svg").read_text()  # a capybara sipping boba
LOGO_URI = "data:image/svg+xml;base64," + base64.b64encode(LOGO.encode()).decode()  # st.html drops inline SVG

st.set_page_config(page_title="CapyEV", page_icon=str(UI / "capyev-icon.png"), layout="wide",
                   initial_sidebar_state="collapsed")
st.html(
    """<style>
    header[data-testid="stHeader"] {display: none;}
    .block-container {padding: 0.4rem 0.5rem 3rem; max-width: 760px;}
    [data-testid="stVerticalBlock"] {gap: 0.25rem;}
    /* The bottom tab bar, like Kalshi's app: the pages around a cyan capybara button that goes Home. */
    .block-container {padding-bottom: calc(96px + var(--host-lift, 0px) + env(safe-area-inset-bottom));}
    /* Streamlit Cloud's badges sit over the app's bottom right. HOST_BADGES_JS sets --host-right to keep the
       tabs left of them, or --host-lift to raise the tabs above them when they're too wide for that. */
    .st-key-tabbar {position: fixed; left: 0; right: 0; bottom: 0; z-index: 999990; gap: 0;
        align-items: flex-end; --capy: clamp(46px, calc((100vw - var(--host-right, 0px)) / 5 - 4px), 58px);
        justify-content: space-around; padding: 6px max(6px, calc(50vw - 380px), var(--host-right, 0px))
        calc(6px + var(--host-lift, 0px) + env(safe-area-inset-bottom)) max(6px, calc(50vw - 380px));
        background: rgba(3, 6, 8, 0.985); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
        border-top: 1px solid #12222a;}
    .st-key-tabbar > div {flex: 1 1 0; min-width: 0; width: auto !important; display: flex; justify-content: center;}
    .st-key-tabbar > div > div {width: 100%; align-items: center;}
    .st-key-tabbar [data-testid="stPageLink"] a {flex-direction: column; gap: 1px; padding: 2px 0; min-height: 0;
        background: none !important; border: 0;}
    .st-key-tabbar [data-testid="stPageLink"] a span {color: #5d7a83; font-size: 23px;}
    .st-key-tabbar [data-testid="stPageLink"] a p {font-size: 11px; font-weight: 800; color: #5d7a83;}
    .st-key-tabbar .st-key-tab_on [data-testid="stPageLink"] a span,
    .st-key-tabbar .st-key-tab_on [data-testid="stPageLink"] a p {color: #e6fbff;}
    .st-key-tabbar .st-key-tab_on [data-testid="stPageLink"] a span[data-testid="stIconMaterial"] {color: #00e5ff;}
    .st-key-tabbar [class*="st-key-capy"] a {width: var(--capy); height: var(--capy); margin-top: calc(var(--capy) * -0.41);
        padding: 0; border-radius: 50%;
        justify-content: center; background: radial-gradient(circle at 35% 30%, #7ff4ff 0%, #00e5ff 38%, #00a9c4 100%) !important;
        border: 3px solid #000 !important; box-shadow: 0 0 0 1.5px rgba(0, 229, 255, 0.55), 0 0 22px rgba(0, 229, 255, 0.45),
        0 6px 14px rgba(0, 0, 0, 0.6);}
    .st-key-tabbar [class*="st-key-capy"] a::before {content: ""; width: calc(var(--capy) * 0.72); height: calc(var(--capy) * 0.72);
        background: url("__LOGO_URI__") center / contain no-repeat;}
    .st-key-tabbar [class*="st-key-capy"] a > * {display: none;}
    .st-key-tabbar .st-key-capy_on a {box-shadow: 0 0 0 2px #e6fbff, 0 0 28px rgba(0, 229, 255, 0.7),
        0 6px 14px rgba(0, 0, 0, 0.6);}
    </style>""".replace("__LOGO_URI__", LOGO_URI)
)

combo_table = st.components.v2.component(
    "combo_table", css=(UI / "table.css").read_text(),
    js=(UI / "table.js").read_text().replace('"__LOGO__"', json.dumps(LOGO)))
home_cards = st.components.v2.component(
    "home_cards", css=(UI / "home.css").read_text(),
    js=(UI / "home.js").read_text().replace('"__LOGO__"', json.dumps(LOGO)))
bettor_table = st.components.v2.component(
    "bettor_table", css=(UI / "table.css").read_text() + (UI / "bettors.css").read_text(),
    js=(UI / "bettors.js").read_text().replace('"__LOGO__"', json.dumps(LOGO)))
rank_table = st.components.v2.component(
    "rank_table", css=(UI / "table.css").read_text() + (UI / "bettors.css").read_text() + (UI / "ranks.css").read_text(),
    js=(UI / "ranks.js").read_text().replace('"__LOGO__"', json.dumps(LOGO)))
live_table = st.components.v2.component(
    "live_table", css=(UI / "table.css").read_text() + (UI / "bettors.css").read_text() + (UI / "ranks.css").read_text(),
    js=(UI / "live.js").read_text().replace('"__LOGO__"', json.dumps(LOGO)))

# The home-screen icon and name. Streamlit has no setting for them, and Streamlit Cloud shows the
# app inside a page of its own, so they're also set on that page when the browser allows it. The
# icon is a static file, served once the app has restarted with static serving on (config.toml);
# until then the tab icon stands in.
TOUCH_ICON_JS = """<script>
(() => {
  const docs = [document];
  try { if (window.top !== window && window.top.document.head) docs.push(window.top.document); } catch (e) {}
  const set = (href) => {
    for (const d of docs) {
      const tag = (sel, name, attrs) => d.head.querySelector(sel) || d.head.appendChild(Object.assign(d.createElement(name), attrs));
      if (href) tag('link[rel="apple-touch-icon"]', "link", { rel: "apple-touch-icon" }).href = href;
      tag('meta[name="apple-mobile-web-app-title"]', "meta", { name: "apple-mobile-web-app-title" }).content = "CapyEV";
    }
  };
  const touch = new URL("app/static/capyev-touch.png", document.baseURI).href;
  const tab = () => (document.querySelector("link[rel='shortcut icon']") || {}).href;
  fetch(touch, { method: "HEAD" })  // without static serving this is the app's HTML page, not the icon
    .then((r) => set((r.headers.get("content-type") || "").startsWith("image/") ? touch : tab()), () => set(tab()));
})();
</script>"""

# Streamlit Cloud draws its badges (the Streamlit logo, the creator's avatar, and "Manage app" for the
# owner) on its own page, over the bottom right of the app, where they caught taps meant for the
# right-hand tabs. They can't be moved from inside the app, so this measures them and sets
# --host-right, which keeps the tabs to their left, or --host-lift, which raises the bar above them
# when keeping left would squeeze the tabs too much. Elsewhere it sets nothing.
HOST_BADGES_JS = """<script>
(() => {
  if (window.__capyHostBadges) return;  // once per page load; the observers keep it current
  window.__capyHostBadges = true;
  let host, frame;
  try { if (window.top !== window) { host = window.top; frame = window.frameElement; } } catch (e) {}
  if (!host || !frame) return;
  const root = document.documentElement;
  let queued = false;
  const fit = () => {
    queued = false;
    const H = host.innerHeight, W = host.innerWidth, box = frame.getBoundingClientRect();
    let left = W, top = H;
    for (const el of host.document.body.querySelectorAll("*")) {
      if (el === frame || el.contains(frame)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8 || r.width > W * 0.6 || r.bottom < H - 120 || r.top > H || r.right < 0) continue;
      const cs = host.getComputedStyle(el);
      if (cs.position === "static" || cs.position === "relative" || cs.visibility === "hidden" || cs.display === "none"
          || +cs.opacity === 0 || cs.pointerEvents === "none") continue;
      left = Math.min(left, r.left);
      top = Math.min(top, r.top);
    }
    const room = left < W ? Math.max(0, Math.ceil(box.right - left) + 4) : 0;
    const side = room <= box.width * 0.4;  // past that, five tabs get too narrow to tap
    root.style.setProperty("--host-right", (side ? room : 0) + "px");
    root.style.setProperty("--host-lift", (side || top >= H ? 0 : Math.max(0, Math.ceil(box.bottom - top) + 4)) + "px");
  };
  const later = () => { if (!queued) { queued = true; host.requestAnimationFrame(fit); } };
  fit();
  new host.MutationObserver(later).observe(host.document.body, { childList: true, subtree: true, attributes: true });
  host.addEventListener("resize", later);
  [500, 1500, 4000, 10000].forEach((ms) => setTimeout(later, ms));  // the badges slide in after the app
})();
</script>"""


def _stop_collectors(name: str = "combo-collector") -> None:
    """Stop workers left running by an earlier version of the app, so only one polls Kalshi."""
    for thread in threading.enumerate():
        if thread.name == name and hasattr(thread, "stop"):
            thread.stop()


@st.cache_resource
def get_source() -> SimpleNamespace:
    """One scorer and background worker shared by every visitor.

    The collector thread pages new combo trades each minute and, every SCORE_SECONDS,
    re-scores the views people have open. Pages only read its latest results.
    """
    if DEMO:
        client = DemoClient(n_trades=1500)  # enough rows to exercise endless scroll
        scorer = Scorer(client, hide=financial.is_financial)
        trades_for = lambda m: client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=m))  # noqa: E731
        return SimpleNamespace(scorer=scorer, trades_for=trades_for, collector=None,
                               board=Board(scorer, trades_for, max_bets=MAX_BETS, default_view=HOME_VIEW,
                                           keep=(DEFAULT_VIEW,)))
    _stop_collectors()
    client = KalshiClient(max_workers=8)  # a new filter's lookups go out 8 at a time
    scorer = Scorer(client, hide=financial.is_financial)  # Kenny bets sports: no crypto or gold legs
    # Load the newest 15 minutes first; older history fills in a poll at a time.
    collector = Collector(client, max_window_minutes=MAX_WINDOW, initial_backfill_minutes=15)
    board = Board(scorer, collector.window, every=SCORE_SECONDS, max_bets=MAX_BETS, default_view=HOME_VIEW,
                  keep=(DEFAULT_VIEW,), wait_for_trades=TRADES_WAIT)
    collector.after_poll = lambda col: board.refresh(trades_as_of=col.poll_started)
    collector.after_error = lambda col: board.poll_failed()
    collector.start()
    return SimpleNamespace(scorer=scorer, trades_for=collector.window, collector=collector, board=board)


def _poll_error(err: str) -> str:
    if "429" in err:
        return "Kalshi is rate limiting us for a moment, so the list is slowing down and will catch up by itself."
    return f"Last Kalshi poll failed, retrying: {err[:150]}"


@st.cache_data(ttl=DEPTH_SECONDS, show_spinner=False)
def with_depth(ticker: str, side: str, lookback: int) -> dict:
    """Re-score one bet against the full orderbook of each leg (list rows use top of book)."""
    src = get_source()
    return src.scorer.rescore_with_depth({"ticker": ticker, "side": side}, src.trades_for(lookback))


# ---------- State the browser sends back ----------
st.session_state.setdefault("min_bet", DEFAULT_VIEW[1])
st.session_state.setdefault("window", DEFAULT_VIEW[0])
st.session_state.setdefault("deep", {})


def _sync(name: str):
    def cb():
        value = getattr(st.session_state.cev, name, None)
        if value is not None:
            st.session_state[name] = value
    return cb


def _refresh():
    """Refresh button: poll Kalshi now and re-score this view in the background.

    The table keeps showing the current bets meanwhile; refresh_watch() swaps in
    the new ones when they're ready.
    """
    src = get_source()
    view = (st.session_state.window, st.session_state.min_bet)
    st.session_state.deep = {}
    st.session_state.refresh_req = {"since": src.board.request(*view), "view": view, "tap": st.session_state.cev.refresh,
                                    "done": False, "slow": False, "error": None}
    if src.collector is not None:
        src.collector.poll_now()  # the worker scores the view right after this poll
    else:  # demo: no collector, so score on a thread of its own the same way
        threading.Thread(target=src.board.refresh, daemon=True).start()
    st.rerun()  # a full run, so refresh_watch() starts checking


def _depth():
    st.session_state.depth_req = st.session_state.cev.depth


def _quote():
    st.session_state.quote_req = st.session_state.cev.quote


def _touch_combos(board) -> None:
    """Keep the Combos view open so the worker keeps it fresh, and the other Min bets at its window,
    so switching is instant. Before the phone's saved filter has come in (the page's first run),
    the other Min bets are held for 5 minutes only, in case it's another window."""
    window, min_bet = st.session_state.window, st.session_state.min_bet
    board.touch(window, min_bet)
    for other in MIN_BETS:
        board.touch(window, other, hold=None if st.session_state.get("table_ran") else 300)
    st.session_state.table_ran = True


def _brew(view: tuple[int, float], message: str):
    """The view's scores and an error, brewing them first if there are none yet or they're old
    (the app sat idle). If brewing fails, the old scores stay up with the error."""
    board = get_source().board
    snap = board.peek(*view)
    if snap is not None and time.time() - snap.fetched_at.timestamp() <= STALE_SECONDS:
        return snap, None
    try:
        with st.spinner(message):
            return board.get(*view, max_age=STALE_SECONDS), None
    except TradesUnavailable:
        return snap, "Kalshi is slow to send the latest trades, so these are the last scores. Tap ↻ in a minute."
    except Exception as exc:
        if snap is None:
            raise
        return snap, f"Couldn't update the scores: {exc}"[:200]


@st.fragment
def table_view():
    go = st.session_state.pop("quote_req", None)
    if go:  # "Bet in CapyEV" on an open bet: check and quote it on the Quote page (quote_view reads these params)
        st.session_state.pop("q_ctx", None)  # even when it's the combo already open there
        st.switch_page(QUOTE, query_params={"t": go["t"], "side": go["side"], "p": f"{go['p']:.4f}",
                                               "n": f"{go['n']:g}"})
    min_bet, window = st.session_state.min_bet, st.session_state.window
    src = get_source()
    notes, error = [], None
    # Keep showing the scores you're reading until you refresh or change the filter, even when
    # this reruns for something else (opening a full book, say) and the worker has newer ones.
    pinned = st.session_state.get("pinned")
    snap = pinned["snap"] if pinned and pinned["view"] == (window, min_bet) else None
    try:
        if snap is None:  # first look at this filter; afterwards the worker keeps it fresh
            snap, error = _brew((window, min_bet), "Brewing scores for this filter… 🧋")
            st.session_state.pinned = {"view": (window, min_bet), "snap": snap}
        _touch_combos(src.board)
    except Exception as exc:  # network / API errors should not blank the page
        snap, error = None, f"Couldn't score bets: {exc}"[:200]

    collector = src.collector
    if collector is not None:
        covered = collector.coverage_minutes()
        if covered < window - 1:
            notes.append(f"Still loading history: {covered:.0f} of {window} min so far.")
        if collector.last_error:
            error = error or _poll_error(collector.last_error)
    if snap and snap.bets_over_min > MAX_BETS:
        about = "about " if getattr(snap, "over_min_estimated", False) else ""  # pinned before a deploy: no field
        notes.append(f"Showing the newest {snap.bets_scored:,} of {about}{snap.bets_over_min:,} bets ≥ ${min_bet:,}.")

    # The browser spins the refresh icon until `refreshed` echoes its tap. Until then the
    # table's data stays exactly as it was, so a rerun resends almost nothing.
    asked = st.session_state.get("refresh_req")
    if asked and (asked["done"] or asked["view"] != (window, min_bet)):
        st.session_state.refreshed = asked["tap"]
        if asked["view"] == (window, min_bet) and snap and snap.fetched_at.timestamp() < asked["since"]:
            if asked["error"]:
                error = error or f"Refresh failed: {asked['error']}"[:200]
            elif asked["slow"]:
                notes.append("Kalshi is slow right now, so these are still the earlier scores. "
                             "Tap ↻ again in a minute.")

    deep = {k: v for k, v in st.session_state.deep.items() if time.time() - v["at"] < DEEP_TTL}
    depth_error = None
    req = st.session_state.pop("depth_req", None)
    if req:
        ticker, side = req.split("|")
        try:
            r = with_depth(ticker, side, window)
            if "leg_detail" not in r:
                raise LookupError("this bet is no longer in the window")
            price = r["price"]
            deep[req] = {"at": time.time(), "legs": slim_legs(r), "dr": num(r["min_depth_ratio"], 3),
                         "evs": num(r["fair_at_size"] / price - 1) if r["fair_at_size"] and price else None}
        except Exception as exc:
            depth_error = {"id": req, "msg": str(exc)[:160]}
    st.session_state.deep = deep

    packed = pack(snap.rows if snap else [])
    combo_table(
        key="cev",
        data={
            **packed, "minBet": min_bet, "window": window, "demo": DEMO,
            "trades": snap.combo_trades_seen if snap else 0, "scored": len(packed["rows"]),
            "updated": int((snap.fetched_at if snap else datetime.now(timezone.utc)).timestamp() * 1000),
            "note": " ".join(notes), "error": error, "deep": deep, "depthError": depth_error,
            "refreshed": st.session_state.get("refreshed"),
        },
        default={"min_bet": min_bet, "window": window},
        on_min_bet_change=_sync("min_bet"),
        on_window_change=_sync("window"),
        on_refresh_change=_refresh,
        on_depth_change=_depth,
        on_quote_change=_quote,
    )


def refresh_watch() -> None:
    """After a refresh tap, check for the new scores every second, then rerun the page once to show them."""
    req = st.session_state.get("refresh_req")
    if req and not req["done"]:
        _watch_refresh()


@st.fragment(run_every=1)
def _watch_refresh() -> None:
    req = st.session_state.get("refresh_req")
    if req and not req["done"]:
        done, failure = get_source().board.done(*req["view"], since=req["since"])
        moved = req["view"] != (st.session_state.window, st.session_state.min_bet)
        if not (done or moved or time.time() - req["since"] > REFRESH_WAIT):
            return
        req.update(done=True, error=failure, slow=not done)
        pinned = st.session_state.get("pinned")
        if pinned and pinned["view"] == req["view"]:
            del st.session_state.pinned  # so the table takes the new scores
    st.rerun()  # a full run shows the new scores and stops this timer


@st.fragment(run_every=60)
def _heartbeat() -> None:
    """Tells the background workers a page is open, so they keep polling Kalshi (see activity.py),
    and keeps an open Combos filter scored while you read it."""
    activity.seen()
    if st.session_state.get("page") == COMBOS.title and st.session_state.get("table_ran"):
        _touch_combos(get_source().board)


def nav() -> None:
    """The tab bar fixed to the bottom of every page (the Streamlit header is hidden), with Home in the middle."""
    _heartbeat()
    st.html(TOUCH_ICON_JS + HOST_BADGES_JS, unsafe_allow_javascript=True)
    here = st.session_state.get("page")
    half = len(NAV_TABS) // 2
    with st.container(horizontal=True, key="tabbar"):
        for i, (page, label, icon) in enumerate(NAV_TABS):
            if i == half:
                with st.container(key="capy_on" if here == HOME.title else "capy", width="content"):
                    st.page_link(HOME, label="Home")
            with st.container(key="tab_on" if here == page.title else f"tab_{label.lower()}", width="content"):
                st.page_link(page, label=label, icon=icon)


def combo_page() -> None:
    nav()
    table_view()

    with st.expander("How EV, units and flags work"):
        st.markdown(
            "- Each card's top line is the bet @ the multiplier it got (bets of $500+ in cyan), its Kelly units and "
            "**EV mid / EV cross**; under them, the fair multipliers behind the two EVs (mid / cross, the one the "
            "switch picks brighter) and the clock time it was bet. **EV mid** multiplies each leg's mid price to "
            "get a fair price, then EV = fair ÷ price paid − 1 (on the card, multiplier paid ÷ fair multiplier − 1). "
            "That assumes the legs are independent. **EV cross** "
            "prices each leg by crossing the spread (its bid). It's the conservative number.\n"
            "- The **EV Mid / Cross** switch picks which one is big, colors the bet, and drives Min EV, Min QK and the "
            "EV and QK sorts; the other figure gets a red chip when it's negative (a green one when it's positive "
            "on a red pill). The green brightens as the edge grows, on the same scale as Home: a pale wash under "
            "+10%, deep green from +10%, bright green from +20%, neon with a 🔥 from +200%, and a ringed, shining "
            "badge from +500%. The **?** button at the top shows this key.\n"
            "- **Units** (next to the EV) = Kelly fraction (p − price) ÷ (1 − price), times your Kelly multiplier, "
            "divided by your unit size. Set both with the gear icon. The defaults are ¼ Kelly and 1u = 1% of bankroll.\n"
            "- **Pre-match / Live** uses each game's scheduled start from Kalshi's schedule: a combo is live once "
            "any leg's game has started. Pre-match bets show when their first game starts. Legs Kalshi "
            f"doesn't schedule count as live within {LIVE_HOURS}h of their expected settlement.\n"
            "- The **filters button** next to search holds Min QK, Min bet, Window, **Games** and **Quotes**. Its "
            "badge counts Min QK, Indep. only and Hide thin when they're on; Min bet and Window show in the line "
            "under the title. **Indep. only** hides combos with two legs on the same game (they're "
            "correlated, so the product of leg prices understates the true odds). **Hide thin** hides combos with a "
            "leg whose spread is wide or whose top of book is small, since its mid isn't a reliable price. Set the "
            "cutoffs with the gear icon; thin quotes show in yellow with *.\n"
            "- **Sort** by Newest (every refresh starts here), EV, Bet $ or QK. Tap the lit one again to flip it.\n"
            "- **Tags**: NO the bettor took NO on the combo · SG two legs on the same game · ⚠ THIN see Hide thin · "
            "IN BAND the price sits inside the leg spreads, so the edge is noise · NO QUOTE a leg has no market · "
            "CHECK an edge of +200% or more, usually a stale book. Then the leagues and legs; when the line is tight, "
            "leagues show as their emoji, or are left to the open bet.\n"
            "- **Open a bet** to see its legs, and the multiplier the bettor paid next to the fair ones behind EV mid "
            "and EV cross (with American odds and units).\n"
            "- **Bet in CapyEV** (on an open bet) opens it on the Quote page, which asks makers for quotes at your "
            "Kelly size and bets the best quote once you confirm (needs your Kalshi key and TRADE_PIN). **kalshi.com** opens "
            "Kalshi's combo builder in your browser with the combo in the slip; the Kalshi app can't open a combo "
            "from a link, and it only buys YES on a combo. Either way the price is quoted fresh, so check it still beats the fair odds. The book "
            "icon walks each leg's whole orderbook for the bet's size.\n"
            "- **↻ Refresh** pulls the newest trades and leg quotes from Kalshi and re-scores this view, usually in "
            "10–15 seconds. The icon spins until the new bets are in; the list never changes on its own.\n"
            "- Fair prices use the order book **now**, so they answer \"is this still good to copy?\". "
            "Kalshi's trade feed has no bettor ID, so these bets are anonymous (the Bettors page covers public profiles)."
        )
    refresh_watch()


# ---------- Home page ----------


def _home_refresh():
    """Home's refresh button: poll Kalshi now and re-score Home's view; _watch_home() shows it when done."""
    src = get_source()
    st.session_state.home_req = {"since": src.board.request(*HOME_VIEW), "tap": st.session_state.home.refresh}
    if src.collector is not None:
        src.collector.poll_now()
    else:  # demo: no collector, so score on a thread of its own
        threading.Thread(target=src.board.refresh, daemon=True).start()
    st.rerun()  # a full run, so home_page() starts _watch_home()


def _home_quote():
    st.session_state.quote_req = st.session_state.home.quote


@st.fragment
def home_view():
    go = st.session_state.pop("quote_req", None)
    if go:  # "Bet in CapyEV" on a card: check and quote it on the Quote page
        st.session_state.pop("q_ctx", None)  # even when it's the combo already open there
        st.switch_page(QUOTE, query_params={"t": go["t"], "side": go["side"], "p": f"{go['p']:.4f}", "n": f"{go['n']:g}"})
    src = get_source()
    error = None
    try:
        snap, error = _brew(HOME_VIEW, "Brewing the best bets… 🧋")
        src.board.touch(*HOME_VIEW)
    except Exception as exc:  # network / API errors should not blank the page
        snap, error = None, f"Couldn't score bets: {exc}"[:200]
    if src.collector is not None and src.collector.last_error:
        error = error or _poll_error(src.collector.last_error)
    req = st.session_state.get("home_req")
    home_cards(
        key="home",
        data={"rows": pack_home(snap.rows if snap else []), "count": len(snap.rows) if snap else 0,
              "window": HOME_VIEW[0], "minBet": HOME_VIEW[1], "demo": DEMO, "error": error,
              "updated": int((snap.fetched_at if snap else datetime.now(timezone.utc)).timestamp() * 1000),
              "refreshed": req["tap"] if req and req.get("done") else None},
        on_refresh_change=_home_refresh,
        on_quote_change=_home_quote,
    )


@st.fragment(run_every=1)
def _watch_home() -> None:
    req = st.session_state.get("home_req")
    if req and not req.get("done"):
        done, _ = get_source().board.done(*HOME_VIEW, since=req["since"])
        if not done and time.time() - req["since"] < REFRESH_WAIT:
            return
        req["done"] = True
    st.rerun()  # a full run shows the new bets and stops this timer


def home_page() -> None:
    nav()
    home_view()
    req = st.session_state.get("home_req")
    if req and not req.get("done"):
        _watch_home()
    with st.expander("How Home picks bets"):
        st.markdown(
            "- Home shows the combos people bet **$100+ on in the last hour** whose fair price beats what they paid, "
            "best first by **EV cross** (each leg priced by crossing the spread, the conservative number).\n"
            "- The badge shows **EV mid / EV cross**. Its green brightens as the edge grows: a pale wash under +10%, "
            "deep green from +10%, bright from +20%, neon with a 🔥 from +200% and a white ring from +500%. A "
            "negative EV cross gets a red chip. Huge edges are often a stale leg book, so a yellow **check the "
            "book** tag marks anything from +200%.\n"
            "- The **u** figure is Kelly units to bet, at the settings you pick on the Combos tab (gear icon: Kelly "
            "and unit size, plus its Mid / Cross switch). The defaults are ¼ Kelly on EV mid and 1u = 1% of "
            "bankroll.\n"
            "- **Bet in CapyEV** gets makers' quotes on the Quote page and bets the best one once you confirm. **kalshi.com** "
            "opens the combo in Kalshi's combo builder in your browser (the Kalshi app can't open a combo from a "
            "link). Either way it's quoted fresh, so make sure the price still beats the fair odds.\n"
            "- Tap a card for all its legs. The Combos tab has every bet, with more filters and the full book."
        )


# ---------- Bettors page ----------
BETTOR_CYCLE = 900  # seconds between passes over the names you add on the page
BETTOR_TABS = ["Whales", "Tennis", "Live", "Profiles"]
DEFAULT_VARIANT = {"Whales": "a:all", "Tennis": "t:30"}


def _secrets() -> tuple[dict, str | None]:
    """The app's secrets (read on every call: Streamlit reloads them when they're edited)."""
    try:
        return st.secrets.to_dict(), None
    except Exception as exc:  # no secrets, or the secrets aren't valid TOML
        if "No secrets" in str(exc):
            return {}, None
        return {}, f"the app's secrets couldn't be read, so check them for a typo ({exc})"[:240]


def _kalshi_auth() -> tuple[KalshiAuth | None, str | None]:
    """The API key from Streamlit secrets or the environment, and why it couldn't be used, if so."""
    secrets, problem = _secrets()
    auth, missing = kalshi_auth(secrets)
    return auth, (None if auth else problem or missing)


class _Fixed:
    """A Feed that always returns the same data (the demo)."""

    def __init__(self, data):
        self.data, self.error = data, None

    def get(self, force: bool = False):
        return self.data


@st.cache_resource(show_spinner=False)  # may be first called from a callback, where nothing should draw
def bettor_feeds() -> SimpleNamespace:
    """Where the Bettors page reads from: the recorder's files on the bettor-data branch (see
    combo_tracker/recorder.py), opened with the app's Kalshi key, plus the starred bettors."""
    if DEMO:
        client = DemoClient()
        view = build_view(demo_store(client))
        rows = pack_bettors(demo_tracker().snapshot.rows)
        profiles = {"saved": view["saved"], "rows": rows, "live": [], "status": "idle"}
        return SimpleNamespace(view=_Fixed(view), profiles=_Fixed(profiles), tags_feed=_Fixed({"tags": view["tags"]}),
                               tags=None, problem=None)
    auth, problem = _kalshi_auth()
    secret = _secret("BETTOR_DATA_KEY")
    if auth is None and not secret and not problem:
        problem = "add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY (the key the recorder uses) to the app's secrets"
    token = _secret("GH_TOKEN")
    return SimpleNamespace(view=recorder.Feed(recorder.VIEW_URL, auth, secret),
                           profiles=recorder.Feed(recorder.PROFILES_URL, auth, secret),
                           tags_feed=recorder.Feed(recorder.TAGS_URL, plain=True, ttl=120),
                           tags=recorder.GitHubTags(token) if token else None, problem=problem)


def demo_tracker() -> BettorTracker:
    client = DemoClient()
    tracker = BettorTracker(client, every=BETTOR_CYCLE, rfq=demo_rfq_book(client))
    tracker.cycle()
    return tracker


@st.cache_resource(show_spinner=False)  # may be first called from a callback, where nothing should draw
def get_bettors() -> BettorTracker:
    """A small tracker for the names you type on the Profiles tab: it loads only their histories
    (the recorder follows the busiest public tennis names around the clock)."""
    for name in ("bettor-tracker", "rfq-stream", "rfq-matcher"):
        _stop_collectors(name)
    if DEMO:
        return demo_tracker()
    return BettorTracker(KalshiClient(rate_per_sec=5), series=[], every=BETTOR_CYCLE).start()


def _tags() -> set[str]:
    """Starred bettors: the shared list on GitHub (when the app has a token) plus this session's."""
    feeds = bettor_feeds()
    shared = recorder.parse_tags(json.dumps(feeds.tags_feed.get() or {}))
    return shared | set(st.session_state.get("local_tags", ()))


def _tags_shared() -> bool:
    """Whether this session's stars go to GitHub: the app has a token and the session unlocked with
    TRADE_PIN (anyone with the link can open the app; only the PIN holder gets to write to the repo)."""
    feeds = bettor_feeds()
    pin = _secret("TRADE_PIN")
    if feeds.tags is None or not pin:
        return False
    auth, _ = _kalshi_auth()
    return quote_view._unlocked(pin, auth.key_id if auth is not None else "demo")


def _set_tag(tag: str, on: bool | None) -> None:
    """Star (on) or unstar a bettor; None flips it. The state asked for is what's kept, so a tap on a
    stale page can't undo the last one."""
    if not recorder.valid_tag(tag):
        return
    feeds = bettor_feeds()
    local = st.session_state.setdefault("local_tags", set())
    if on is None:
        on = tag not in _tags()
    if _tags_shared():
        try:
            tags = feeds.tags.set(tag, on)
            with feeds.tags_feed.lock:  # the raw file on GitHub lags a few minutes; we know the answer
                feeds.tags_feed.data, feeds.tags_feed.fetched_at, feeds.tags_feed.error = {"tags": sorted(tags)}, time.time(), None
            local.discard(tag)
            return
        except Exception as exc:
            st.session_state.tag_error = f"Couldn't save the star on GitHub: {type(exc).__name__}: {exc}"[:160]
    if on:  # (a statement: a bare conditional expression here would be st.write'n by Streamlit's magic)
        local.add(tag)
    else:
        local.discard(tag)


def _rank_event(name: str):
    def cb():
        value = getattr(st.session_state.rk, name, None)
        if value is None:
            return
        if name == "view":
            st.session_state[f"rk_variant_{st.session_state.get('bt_tab') or 'Whales'}"] = value
        elif name == "open":
            st.session_state.rk_open = value
        elif name == "tag":
            _set_tag(value["id"], value.get("on"))
        elif name == "quote":
            st.session_state.quote_req = value
        elif name == "refresh":
            bettor_feeds().view.get(force=True)
    return cb


def _live_event(name: str):
    def cb():
        value = getattr(st.session_state.lv, name, None)
        if value is None:
            return
        if name == "tag":
            _set_tag(value["id"], value.get("on"))
        elif name == "quote":
            st.session_state.quote_req = value
        elif name == "refresh":
            feeds = bettor_feeds()
            feeds.view.get(force=True)
            feeds.profiles.get(force=True)
    return cb


def _track():
    value = getattr(st.session_state.bt, "track", None)
    name, add = (value.get("name"), value.get("add")) if isinstance(value, dict) else (value, False)
    if name:
        get_bettors().add(name)
        if add and _tags_shared() and name not in _tags():  # a name added here: the recorder follows it too
            _set_tag(name, True)


def _go_quote() -> None:
    go = st.session_state.pop("quote_req", None)
    if go:  # "Quote it" on a bet: check it on the Quote page (quote_view reads these params)
        st.session_state.pop("q_ctx", None)
        st.switch_page(QUOTE, query_params={"t": go["t"], "side": go["side"], "p": f"{go['p']:.4f}", "n": f"{go['n']:g}"})


def _data_note(feeds, view) -> tuple[str | None, str | None]:
    """(error, note) for the recorder's data."""
    if feeds.problem:
        return f"RFQ codes need the recorder's key: {feeds.problem}.", None
    if view is None:
        return feeds.view.error or "No data yet.", None
    note = None
    if feeds.view.error:
        note = f"Showing the last copy: {feeds.view.error}"
    elif view.get("saved") and time.time() * 1000 - view["saved"] > 30 * 60000:
        note = "The recorder hasn't saved in a while; it may be between runs. Start it from Actions → Bettor recorder."
    return None, note


@st.fragment
def bettors_view():
    _go_quote()
    feeds = bettor_feeds()
    tab = st.segmented_control("Tab", BETTOR_TABS, default="Whales", key="bt_tab", label_visibility="collapsed",
                               width="stretch") or "Whales"
    tags = _tags()
    if tab in ("Whales", "Tennis"):
        view = feeds.view.get()
        error, note = _data_note(feeds, view)
        variant = st.session_state.get(f"rk_variant_{tab}", DEFAULT_VARIANT[tab])
        data = pack_view(view, variant, open_code=st.session_state.get("rk_open"), tags=tags)
        rank_table(key="rk", data={**data, "tab": tab.lower(), "demo": DEMO, "error": error,
                                   "note": " ".join(filter(None, [note, st.session_state.pop("tag_error", None)]))},
                   on_view_change=_rank_event("view"), on_open_change=_rank_event("open"), on_tag_change=_rank_event("tag"),
                   on_quote_change=_rank_event("quote"), on_refresh_change=_rank_event("refresh"))
    elif tab == "Live":
        view = feeds.view.get()
        profiles = feeds.profiles.get() or {}
        error, note = _data_note(feeds, view)
        data = pack_live(view, tags=tags)
        seen = {b["id"] for b in data["live"]}  # a named code who is also a tracked profile: the code's copy has the record
        by_nick = {r["nick"]: r for r in profiles.get("rows") or []}  # the profiles' own 30-day records
        data["live"] = sorted(data["live"] + [{**b, "c": None, "tg": b.get("name") in tags,
                                               "s30": by_nick.get(b.get("name"), {}).get("all30"),
                                               "t30": by_nick.get(b.get("name"), {}).get("tennis30")}
                                              for b in profiles.get("live") or [] if b["id"] not in seen],
                              key=lambda b: -b["t"])
        live_table(key="lv", data={**data, "demo": DEMO, "error": error, "note": note},
                   on_tag_change=_live_event("tag"), on_quote_change=_live_event("quote"), on_refresh_change=_live_event("refresh"))
    else:
        tracker = get_bettors()
        profiles = feeds.profiles.get() or {}
        rows = {r["nick"]: r for r in profiles.get("rows") or []}
        snap = tracker.snapshot
        if snap:  # names added here take precedence: they're fresher
            rows.update((r["nick"], r) for r in pack_bettors(snap.rows))
        notes = []
        if profiles.get("feed_since"):
            notes.append(f"Tennis feed since {datetime.fromtimestamp(profiles['feed_since'] / 1000, timezone.utc):%H:%M} UTC: "
                         f"{profiles.get('named_trades', 0):,} of {profiles.get('feed_trades', 0):,} trades show a public name.")
        if feeds.problem:
            notes.append(f"Recorder data needs the key: {feeds.problem}.")
        elif feeds.profiles.error and not rows:
            notes.append(feeds.profiles.error)
        bettor_table(
            key="bt",
            data={
                "rows": list(rows.values()), "demo": DEMO,
                "updated": int((profiles.get("saved") or (snap.updated.timestamp() * 1000 if snap else time.time() * 1000))),
                "status": tracker.status if snap else "idle", "note": " ".join(notes),
                "error": ("Some Kalshi calls failed and will be retried: " + snap.errors[-1][:160]) if snap and snap.errors else None,
            },
            on_refresh_change=lambda: feeds.profiles.get(force=True),
            on_track_change=_track,
        )


def bettors_page() -> None:
    nav()
    bettors_view()
    with st.expander("Where this data comes from and how to read it"):
        st.markdown(
            "- **RFQ codes**: every combo bet starts as a request for quote, which Kalshi broadcasts to logged-in "
            "users with the requester's code (a hash that stays the same for that person). When the bet fills, Kalshi "
            "closes the request in the same instant, so the trade is matched to the request on the same market, with "
            "the same size, that closed with it. About 97% of combo bets get a code this way. A **recorder** runs "
            "around the clock in GitHub Actions doing this, prices each kept bet against its legs' books seconds "
            "after it fills, settles it when the combo resolves, and saves everything (encrypted) every few minutes. "
            "When a bet's trade shows a public nickname in Kalshi's social feed, the code gets that name.\n"
            "- **Whales**: anyone who has placed a combo bet of $5,000 or more; from then on their bets of $1,000+ "
            "are kept too. The size chips filter by their biggest bet in the period. **95¢+ bets** are locks (risking "
            "a lot to win a little, usually on a combo that's nearly decided); they're left out unless you turn them on, "
            "because they swamp the totals.\n"
            "- **Tennis**: every combo bet with a tennis leg of $25 or more is kept, for everyone, so tennis bettors "
            "can be ranked with a real sample. Default view: last 30 days, 5+ bets, sorted by z.\n"
            "- **Size** is dollars bet in the period, with the biggest single bet under it. **PnL** is what their settled "
            "bets made after fees (ROI under it); *open* means nothing has settled yet. **vs EV** = settled PnL − what "
            "the book said those bets were worth when placed (fair value from the legs' mids), and **z** = that ÷ its "
            "standard deviation: around 0 is what the book expected, 2+ is hard to get by luck. **Bets** counts them, "
            "with the share won under it.\n"
            "- **Live**: the open bets from the last two days of whales, the top tennis bettors, anyone you starred, "
            "and the public profiles below, newest first, each with the bettor's 30-day record. **Quote it** checks "
            "the same combo on the Quote page.\n"
            "- **★ Star** a bettor to keep every bet of theirs (whatever the size) and to filter to them. Stars are "
            "kept on this phone; with a GitHub token (GH_TOKEN) in the app's secrets, and once you've unlocked with "
            "the PIN on the Quote page, they're shared with the recorder, which can also send each starred bettor's "
            "bets to Telegram.\n"
            "- **Profiles**: public Kalshi profiles (the busiest names in the tennis feed plus any you add) with their "
            "whole trade history, Kalshi's own PnL, EV against one-minute candles, and **Beat** = PnL − EV with its z. "
            "A profile that hides its trades shows **hidden**.\n"
            "- Combos with a crypto, commodity or other financial leg aren't recorded at all, as on the other pages.\n"
            "- Bets older than a few days of ordinary bettors are folded into per-day totals; whales' and starred "
            "bettors' bets are kept whole for a year. The recorder saves every 5 minutes, and the page re-reads every 5."
        )


# ---------- Quote page ----------
@st.cache_resource
def quote_client():
    """Its own client and rate limit, so checking a quote never waits behind the collector."""
    return KalshiClient(rate_per_sec=8)


def _leg_pool(scorer, max_age: float = 900):
    """The leg markets and events the scorer has fetched in the last 15 minutes: legs people
    combined recently, which the Quote page suggests as extra legs."""
    def pool():
        now = time.time()
        with scorer.leg_markets.lock:
            markets = {k: v for k, (t, v) in scorer.leg_markets.data.items() if now - t < max_age}
        with scorer.events.lock:
            events = {k: v for k, (t, v) in scorer.events.data.items()}
        return markets, events
    return pool


def _secret(name: str) -> str | None:
    """A secret, found the same forgiving way as the Kalshi key; blank counts as not set."""
    return find_secret(_secrets()[0], name)


def quote_page() -> None:
    nav()
    src = get_source()
    view = (st.session_state.get("window", DEFAULT_VIEW[0]), st.session_state.get("min_bet", DEFAULT_VIEW[1]))
    snap = src.board.peek(*view) or src.board.peek(*DEFAULT_VIEW) or src.board.peek(*HOME_VIEW)  # always scored
    if snap is None:
        with st.spinner("Loading recent bets…"):
            snap = src.board.get(*view)
    rows = sorted(snap.rows, key=lambda r: r["last_time"], reverse=True) if snap else []
    client = src.scorer.client if DEMO else quote_client()  # the demo's bets only exist in its own client
    auth, problem = (None, None) if DEMO else _kalshi_auth()
    problem = f"Quotes are off: {problem}." if problem else None
    pool = (lambda: (client.leg_markets, client.events)) if DEMO else _leg_pool(src.scorer)
    quote_view.render(client, rows, starts=src.scorer.starts, auth=auth, demo=DEMO, pool=pool,
                      scored_at=snap.fetched_at if snap else None, pin=_secret("TRADE_PIN"), auth_problem=problem)
    with st.expander("How to read this"):
        st.markdown(
            "- **Paste ticker**: copy a combo's ticker on the Combos page (open a bet → Copy ticker) and tap the big "
            "button. A CapyEV quote link works too. The bet's price and time come from the Combos list when it's "
            "there, otherwise from the combo's newest fill.\n"
            "- A fill in the feed means someone got that price, not that you still can. This page checks one combo "
            "**now**. **LIVE**: the combo's own orderbook has resting orders at or under the price, or it filled at "
            "that price in the last 90 seconds. **LIKELY**: its newest fill was at or under the price, but a few "
            "minutes ago. **MOVED**: newer fills are at a worse price. **STALE**: nothing on this side in 10 minutes.\n"
            "- **Since HH:MM** compares the legs now with when CapyEV scored the bet: fair odds, EV at the bet's "
            "price, and the legs whose mid moved (▲ helps a YES bet, ▼ hurts it). A +EV bet from two minutes ago "
            "can be −EV now if a leg moved.\n"
            "- **Quotes**: pasting a combo asks every maker for a quote at the size you pick (Kelly by default, in "
            "units; fees included). Quotes show the odds after your fee, cost, EV at the legs' mid and cross, and "
            "Kelly units at that price. A request closes after 45 seconds; tap Get quotes for fresh ones.\n"
            "- **Bet** takes the best quote after you confirm it: you buy at the maker's price for the quoted size, "
            "the maker confirms within 3 seconds, and the fill shows here and in the Kalshi app. It needs the "
            "TRADE_PIN secret, since anyone with the link can open this app.\n"
            "- **Add legs**: makers price whole combos, not legs, so adding a near-certain leg on another game "
            "sometimes lowers the price by less than the leg's odds. Pick legs, quote the bigger combo, and compare "
            "EV with the original's quotes.\n"
            "- **Bet on kalshi.com** opens Kalshi's combo builder in your browser with this combo in the slip (the "
            "Kalshi app can't open a combo from a link). kalshi.com only buys YES on a combo, so bet a NO here."
        )


# ---------- Tools page ----------
def tools_page() -> None:
    nav()
    tools_view.render()


HOME = st.Page(home_page, title="Home", default=True)
COMBOS = st.Page(combo_page, title="Combos", url_path="combos")
BETTORS = st.Page(bettors_page, title="Bettors", url_path="bettors")
QUOTE = st.Page(quote_page, title="Quote", url_path="quote")
TOOLS = st.Page(tools_page, title="Tools", url_path="tools")
# The tabs either side of the capybara (Home) button, left to right.
NAV_TABS = [(COMBOS, "Combos", ":material/format_list_bulleted:"),
            (BETTORS, "Bettors", ":material/groups:"),
            (QUOTE, "Quote", ":material/request_quote:"),
            (TOOLS, "Tools", ":material/build:")]
current = st.navigation([HOME, COMBOS, BETTORS, QUOTE, TOOLS], position="hidden")
st.session_state.page = current.title  # nav() lights this page's tab
current.run()
