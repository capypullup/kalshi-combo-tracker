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

from combo_tracker.bettors import BettorTracker  # noqa: E402  (after the reload check)
from combo_tracker.client import KalshiAuth, KalshiClient  # noqa: E402
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.demo import DemoClient, demo_rfq_book  # noqa: E402
from combo_tracker.home import pack_home  # noqa: E402
from combo_tracker.pipeline import Board, Scorer  # noqa: E402
from combo_tracker import quote_view  # noqa: E402
from combo_tracker import tools_view  # noqa: E402
from combo_tracker.rfq import RfqBook  # noqa: E402
from combo_tracker.view import LIVE_HOURS, num, pack, pack_bettors, slim_legs  # noqa: E402

DEMO = os.environ.get("DEMO") == "1"
SCORE_SECONDS = 120  # how often the background worker re-scores the views people have open
DEPTH_SECONDS = 60  # how long a "full book" lookup is cached
MAX_BETS = 600  # newest bets scored per view
MAX_WINDOW = 180  # minutes of combo fills the collector keeps
DEEP_TTL = 180  # seconds a "full book" result stays on an expanded bet
REFRESH_WAIT = 45  # seconds the refresh button waits for new scores before saying Kalshi is slow

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
        scorer = Scorer(client)
        trades_for = lambda m: client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=m))  # noqa: E731
        return SimpleNamespace(scorer=scorer, trades_for=trades_for, collector=None,
                               board=Board(scorer, trades_for, max_bets=MAX_BETS))
    _stop_collectors()
    client = KalshiClient()
    scorer = Scorer(client)
    collector = Collector(client, max_window_minutes=MAX_WINDOW)
    board = Board(scorer, collector.window, every=SCORE_SECONDS, max_bets=MAX_BETS)
    collector.after_poll = lambda col: board.refresh(trades_as_of=col.poll_started)
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
st.session_state.setdefault("min_bet", 100)
st.session_state.setdefault("window", 60)
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


@st.fragment
def table_view():
    go = st.session_state.pop("quote_req", None)
    if go:  # "Quote it" on an open bet: check it on the Quote page (quote_view reads these params)
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
        if snap is None:
            snap = src.board.peek(window, min_bet)
            if snap is None:  # first look at this min bet / window; afterwards it updates in the background
                with st.spinner("Brewing scores for this filter… 🧋"):
                    snap = src.board.get(window, min_bet)
            st.session_state.pinned = {"view": (window, min_bet), "snap": snap}
        src.board.touch(window, min_bet)  # keeps the view open so the worker keeps it fresh
    except Exception as exc:  # network / API errors should not blank the page
        snap, error = None, f"Couldn't score bets: {exc}"[:200]

    collector = src.collector
    if collector is not None:
        covered = collector.coverage_minutes()
        if covered < window - 1:
            notes.append(f"Still loading history: {covered:.0f} of {window} min so far.")
        if collector.last_error:
            error = error or _poll_error(collector.last_error)
    if snap and snap.bets_over_min > snap.bets_scored:
        notes.append(f"Showing the newest {snap.bets_scored:,} of {snap.bets_over_min:,} bets ≥ ${min_bet:,}.")

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


def nav() -> None:
    """The tab bar fixed to the bottom of every page (the Streamlit header is hidden), with Home in the middle."""
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
            "- Each bet leads with **EV mid / EV cross**. **EV mid** multiplies each leg's mid price to get a fair "
            "price, then EV = fair ÷ price paid − 1. That assumes the legs are independent. **EV cross** prices each "
            "leg by crossing the spread (its bid). It's the conservative number.\n"
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
            "CHECK an edge of +200% or more, usually a stale book. On the right: the bet, the multiplier and American "
            "odds it got, and what it pays; bets of $500+ show in cyan.\n"
            "- **Open a bet** to see its legs, and the multiplier the bettor paid next to the fair ones behind EV mid "
            "and EV cross (with American odds and units).\n"
            "- **Bet in Kalshi** (on an open bet) opens that combo in the Kalshi app, or in the combo builder on "
            "kalshi.com if the app isn't installed. Kalshi quotes it fresh, so check the price still beats the fair "
            "odds before you buy. **Quote it** checks the live price on the Quote page instead. The book icon walks "
            "each leg's whole orderbook for the bet's size.\n"
            "- **↻ Refresh** pulls the newest trades and leg quotes from Kalshi and re-scores this view, usually in "
            "10–15 seconds. The icon spins until the new bets are in; the list never changes on its own.\n"
            "- Fair prices use the order book **now**, so they answer \"is this still good to copy?\". "
            "Kalshi's trade feed has no bettor ID, so these bets are anonymous (the Bettors page covers public profiles)."
        )
    refresh_watch()


# ---------- Home page ----------
HOME_VIEW = (60, 100)  # last hour, bets of $100+: a view the worker always keeps scored


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
    if go:  # "Quote" on a card: check it on the Quote page
        st.session_state.pop("q_ctx", None)  # even when it's the combo already open there
        st.switch_page(QUOTE, query_params={"t": go["t"], "side": go["side"], "p": f"{go['p']:.4f}", "n": f"{go['n']:g}"})
    src = get_source()
    error = None
    try:
        snap = src.board.peek(*HOME_VIEW)
        if snap is None:
            with st.spinner("Brewing the best bets… 🧋"):
                snap = src.board.get(*HOME_VIEW)
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
            "- **Bet in Kalshi** opens the combo in Kalshi's app (or its combo builder on the web). Kalshi quotes it "
            "fresh, so make sure the price still beats the fair odds. **Quote** checks the live price here first.\n"
            "- Tap a card for all its legs. The Combos tab has every bet, with more filters and the full book."
        )


# ---------- Bettors page ----------
BETTOR_CYCLE = 900  # seconds between full passes over the tennis feed and bettors' histories


def _kalshi_auth() -> tuple[KalshiAuth | None, str | None]:
    """The API key from Streamlit secrets or the environment, and why it couldn't be used, if so."""
    try:
        secrets = dict(st.secrets)
    except Exception as exc:  # no secrets, or the secrets aren't valid TOML
        secrets = {}
        if "No secrets" not in str(exc):
            return None, f"the app's secrets couldn't be read ({exc})"[:200]
    try:
        return KalshiAuth.from_env(secrets), None
    except Exception as exc:
        return None, f"the private key couldn't be read ({exc})"[:200]


@st.cache_resource
def get_bettors() -> BettorTracker:
    """One tracker shared by every visitor, started the first time someone opens the page.

    With a Kalshi API key it also listens to RFQs and matches them to new combo
    trades, which identifies combo bettors by their RFQ code.
    """
    if DEMO:
        client = DemoClient()
        tracker = BettorTracker(client, every=BETTOR_CYCLE, rfq=demo_rfq_book(client))
        tracker.cycle()
        return tracker
    for name in ("bettor-tracker", "rfq-stream", "rfq-matcher"):
        _stop_collectors(name)
    auth, problem = _kalshi_auth()
    rfq = None
    if auth is not None:
        # Its own client: it polls the trade feed every 5 seconds and looks up hidden requesters.
        rfq = RfqBook(KalshiClient(rate_per_sec=4, auth=auth)).start()
    # Its own client and rate limit, so it never slows the combo collector down.
    tracker = BettorTracker(KalshiClient(rate_per_sec=5), every=BETTOR_CYCLE, rfq=rfq)
    tracker.auth_problem = problem
    return tracker.start()


def _track():
    name = getattr(st.session_state.bt, "track", None)
    if name:
        get_bettors().add(name)


@st.fragment
def bettors_view():
    tracker = get_bettors()
    snap = tracker.snapshot
    notes = []
    if snap and snap.feed_since:
        notes.append(f"Tennis feed since {snap.feed_since:%H:%M} UTC: {snap.named_trades:,} of "
                     f"{snap.feed_trades:,} trades show a public name.")
    rfq = snap.rfq if snap else None
    if rfq is None and not DEMO:
        problem = getattr(tracker, "auth_problem", None)
        notes.append(f"RFQ matching is off: {problem}." if problem else
                     "RFQ matching is off: add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY to the app's secrets.")
    elif rfq:
        matched = rfq.get("sure", 0) + rfq.get("likely", 0)
        bets = rfq.get("executions", 0)
        since = f" since {datetime.fromtimestamp(rfq['since'], timezone.utc):%H:%M} UTC" if rfq.get("since") else ""
        heard = f" from {rfq['rfqs']:,} RFQs" if rfq.get("rfqs") else ""
        notes.append(f"RFQ matching{since}: {matched:,} of {bets:,} combo bets named ({matched / max(bets, 1):.0%}, "
                     f"{rfq.get('sure', 0):,} sure){heard}; {rfq.get('codes', 0):,} codes, "
                     f"the top {rfq.get('shown', 0)} listed, {rfq.get('linked', 0)} linked to a name. "
                     f"Stream {rfq.get('stream')}.")
    bettor_table(
        key="bt",
        data={
            "rows": pack_bettors(snap.rows) if snap else [], "demo": DEMO,
            "updated": int((snap.updated if snap else datetime.now(timezone.utc)).timestamp() * 1000),
            "status": tracker.status, "note": " ".join(notes),
            "error": ("Some Kalshi calls failed and will be retried: " + snap.errors[-1][:160]) if snap and snap.errors else None,
        },
        on_refresh_change=lambda: None,
        on_track_change=_track,
    )


def bettors_page() -> None:
    nav()
    bettors_view()
    with st.expander("Where this data comes from and how to read it"):
        st.markdown(
            "- **Who's listed**: the 40 busiest public names in the last day of Kalshi's tennis trades, any "
            "nickname you add with the search box, and combo bettors identified by **RFQ code** (below).\n"
            "- **RFQ codes**: every combo bet starts as a request for quote, which Kalshi broadcasts to logged-in "
            "users with the requester's code (a hash that stays the same for that person). When the bet fills, Kalshi "
            "closes the request in the same instant, so a combo trade is matched to the request on the same market, "
            "with the same size (or a bit more: a quote can fill less than asked), that closed within a couple of "
            "seconds of it. **Sure** means every request that fit and closed about as near the trade came from the "
            "same person (the app re-asks every few seconds while a slip is open); otherwise the one that closed "
            "nearest the trade wins. Thousands of people bet combos every hour, so only the 100 biggest "
            "codes by dollars bet, plus the 50 biggest in tennis, with 3+ bets are listed and priced. When a matched "
            "trade shows a public nickname in the social feed, the code is linked to that name. Codes only build up "
            "while the app runs (Kalshi doesn't keep RFQ history), and need an API key in the app's secrets.\n"
            "- Kalshi's main trade feed has no trader ID. Its **social API** (what kalshi.com profiles use, free "
            "and keyless) returns the same trades with the maker's and taker's nicknames, but only for profiles "
            "that show their trades. A profile that hides them shows **hidden**, with only Kalshi's own PnL and "
            "markets-traded counts.\n"
            "- **PnL** is Kalshi's number for the profile; **View** switches between all-time and the last 30 days, "
            "with the other one underneath. For an RFQ code it's what its matched, settled bets made. **Trades** "
            "counts the fills in our sample (the bettor's whole history, loaded over a few cycles), and **win %** "
            "is the share of settled ones that won.\n"
            "- **EV** prices every trade against the market's bid/ask mid in the minute it happened (one-minute "
            "candles), after taker fees. A combo is priced from its legs' books. A market maker earns the spread, "
            "so EV is positive; a taker who crosses the spread starts negative.\n"
            "- **Beat** = settled PnL − EV on the same trades, and **z** = that ÷ its standard deviation. z tells "
            "luck from skill: around 0 is what the book expected, 2+ over hundreds of trades is rare by chance. "
            "That's the tennis-sharp signal: PnL far above what the book said their bets were worth.\n"
            "- **Scope** switches the sample between tennis-only and all of the bettor's trades. Tap a row for "
            "their latest trades. Maker fees (some markets charge them) aren't included.\n"
            "- The tracker refreshes every 15 minutes in the background; the first pass after a restart takes "
            "several minutes."
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
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets file
        value = None
    if value is None:
        value = os.environ.get(name)
    return (str(value).strip() or None) if value is not None else None  # blank counts as not set


def quote_page() -> None:
    nav()
    src = get_source()
    view = (st.session_state.get("window", 60), st.session_state.get("min_bet", 100))
    snap = src.board.peek(*view) or src.board.peek(60, 100)  # the worker always keeps 60 min / $100 scored
    if snap is None:
        with st.spinner("Loading recent bets…"):
            snap = src.board.get(*view)
    rows = sorted(snap.rows, key=lambda r: r["last_time"], reverse=True) if snap else []
    client = src.scorer.client if DEMO else quote_client()  # the demo's bets only exist in its own client
    auth, problem = (None, None) if DEMO else _kalshi_auth()
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
            "- **Bet in Kalshi** opens the combo's slip in the Kalshi app (or on kalshi.com) to bet it there. Check the "
            "side in the slip for a NO bet."
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
