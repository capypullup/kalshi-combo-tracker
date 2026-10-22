"""Kalshi combo EV tracker: a phone-first Streamlit dashboard.

Run locally:  streamlit run app.py
Offline demo: DEMO=1 streamlit run app.py

The table itself is a small custom component (combo_tracker/ui). Filters, sorting,
expanding a bet and copying its card all run in the browser. Scoring runs in a
background worker; the page only reads its latest results when the min bet or
window changes, on refresh, and to load a bet's full orderbooks.
"""

from __future__ import annotations

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
from combo_tracker.demo import DemoClient, demo_rfqs  # noqa: E402
from combo_tracker.pipeline import Board, Scorer  # noqa: E402
from combo_tracker.rfq import RfqBook  # noqa: E402
from combo_tracker.view import LIVE_HOURS, num, pack, pack_bettors, slim_legs  # noqa: E402

DEMO = os.environ.get("DEMO") == "1"
SCORE_SECONDS = 120  # how often the background worker re-scores the views people have open
DEPTH_SECONDS = 60  # how long a "full book" lookup is cached
MAX_BETS = 600  # newest bets scored per view
MAX_WINDOW = 180  # minutes of combo fills the collector keeps
DEEP_TTL = 180  # seconds a "full book" result stays on an expanded bet

st.set_page_config(page_title="Combo EV", page_icon="🎯", layout="wide", initial_sidebar_state="collapsed")
st.html(
    """<style>
    header[data-testid="stHeader"] {display: none;}
    .block-container {padding: 0.4rem 0.5rem 3rem; max-width: 760px;}
    [data-testid="stVerticalBlock"] {gap: 0.25rem;}
    .st-key-nav {gap: 6px;}
    .st-key-nav [data-testid="stPageLink"] a {border: 1px solid #1b3540; border-radius: 8px; padding: 2px 12px;
        min-height: 0; font-size: 12px; font-weight: 700; letter-spacing: 0.08em;}
    .st-key-nav [data-testid="stPageLink"] a span {color: #6f8f99;}
    .st-key-nav [data-testid="stPageLink"] a[aria-current="page"] {border-color: #00e5ff; background: rgba(0, 229, 255, 0.14);}
    .st-key-nav [data-testid="stPageLink"] a[aria-current="page"] span {color: #00e5ff;}
    </style>"""
)

UI = Path(__file__).parent / "combo_tracker" / "ui"
combo_table = st.components.v2.component(
    "combo_table", css=(UI / "table.css").read_text(), js=(UI / "table.js").read_text())
bettor_table = st.components.v2.component(
    "bettor_table", css=(UI / "table.css").read_text() + (UI / "bettors.css").read_text(),
    js=(UI / "bettors.js").read_text())


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
    collector.after_poll = lambda col: board.refresh()
    collector.start()
    return SimpleNamespace(scorer=scorer, trades_for=collector.window, collector=collector, board=board)


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
    # The worker keeps scores fresh; tapping refresh just shows its latest results.
    st.session_state.deep = {}
    if DEMO:
        get_source().board.refresh(force=True)


def _depth():
    st.session_state.depth_req = st.session_state.cev.depth


@st.fragment
def table_view():
    min_bet, window = st.session_state.min_bet, st.session_state.window
    src = get_source()
    notes, error = [], None
    try:
        snap = src.board.peek(window, min_bet)
        if snap is None:  # first look at this min bet / window; afterwards it updates in the background
            with st.spinner("Scoring bets for this filter…"):
                snap = src.board.get(window, min_bet)
        else:
            src.board.get(window, min_bet)  # marks the view as open so the worker keeps it fresh
    except Exception as exc:  # network / API errors should not blank the page
        snap, error = None, f"Couldn't score bets: {exc}"[:200]

    collector = src.collector
    if collector is not None:
        covered = collector.coverage_minutes()
        if covered < window - 1:
            notes.append(f"Still loading history: {covered:.0f} of {window} min so far.")
        if collector.last_error:
            error = error or f"Last Kalshi poll failed, retrying: {collector.last_error[:150]}"
    if snap and snap.bets_over_min > snap.bets_scored:
        notes.append(f"Showing the newest {snap.bets_scored:,} of {snap.bets_over_min:,} bets ≥ ${min_bet:,}.")

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
        },
        default={"min_bet": min_bet, "window": window},
        on_min_bet_change=_sync("min_bet"),
        on_window_change=_sync("window"),
        on_refresh_change=_refresh,
        on_depth_change=_depth,
    )


def nav() -> None:
    """Page switcher: two small tabs above the table (the Streamlit header is hidden)."""
    with st.container(horizontal=True, key="nav"):
        st.page_link(PAGES[0], label="COMBOS")
        st.page_link(PAGES[1], label="BETTORS")


def combo_page() -> None:
    nav()
    table_view()

    with st.expander("How EV, units and flags work"):
        st.markdown(
            "- **EV MID** multiplies each leg's mid price to get a fair price, then EV = fair ÷ price paid − 1. "
            "That assumes the legs are independent.\n"
            "- **EV CROSS** prices each leg by crossing the spread (its bid). It's the conservative number.\n"
            "- **Units** = Kelly fraction (p − price) ÷ (1 − price), times your Kelly multiplier, divided by your "
            "unit size. Set both with the gear icon. The defaults are ¼ Kelly and 1u = 1% of bankroll.\n"
            "- **MIN EV / MIN QK** filter on whichever basis the MID / CROSS switch shows. Tap a chip again to clear it.\n"
            "- **INDEP. ONLY** hides combos with two legs on the same game (they're correlated, so the product "
            "of leg prices understates the true odds). Those rows show their legs/games count in yellow.\n"
            "- **PREGAME / LIVE** uses each game's scheduled start from Kalshi's schedule: a combo is live once "
            "any leg's game has started (red dot by the time; open a bet to see each leg's start). Legs Kalshi "
            f"doesn't schedule count as live within {LIVE_HOURS}h of their expected settlement.\n"
            "- **HIDE THIN** hides combos with a leg whose spread is wide or whose top of book is small, since "
            "its mid isn't a reliable price. Set the cutoffs with the gear icon; thin quotes show in yellow with *.\n"
            "- **Full book** (book icon on an open bet) walks each leg's whole orderbook for the bet's size.\n"
            "- **Flags**: THIN see HIDE THIN · IN BAND the price "
            "sits inside the leg spreads, so the edge is noise · NO QUOTE a leg has no market · CHECK an edge "
            "over +200%, usually a stale book.\n"
            "- Fair prices use the order book **now**, so they answer \"is this still good to copy?\". "
            "Kalshi's trade feed has no bettor ID, so these bets are anonymous (the Bettors page covers public profiles)."
        )


# ---------- Bettors page ----------
BETTOR_CYCLE = 900  # seconds between full passes over the tennis feed and bettors' histories


def _kalshi_auth() -> KalshiAuth | None:
    """The API key from Streamlit secrets or the environment; None when it isn't set."""
    try:
        secrets = dict(st.secrets)
    except Exception:  # no secrets file
        secrets = {}
    return KalshiAuth.from_env(secrets)


@st.cache_resource
def get_bettors() -> BettorTracker:
    """One tracker shared by every visitor, started the first time someone opens the page.

    With a Kalshi API key it also listens to RFQs and matches them to the combo
    collector's trades, which identifies combo bettors by their RFQ code.
    """
    if DEMO:
        client = DemoClient()
        rfq = RfqBook(client, lambda m: client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=m)))
        for d in demo_rfqs(client):
            rfq.ingest(d)
        tracker = BettorTracker(client, every=BETTOR_CYCLE, rfq=rfq)
        tracker.cycle()
        return tracker
    for name in ("bettor-tracker", "rfq-stream"):
        _stop_collectors(name)
    auth = _kalshi_auth()
    rfq = None
    if auth is not None:
        rfq = RfqBook(KalshiClient(rate_per_sec=3, auth=auth), get_source().collector.window).start()
    # Its own client and rate limit, so it never slows the combo collector down.
    return BettorTracker(KalshiClient(rate_per_sec=5), every=BETTOR_CYCLE, rfq=rfq).start()


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
        notes.append("RFQ matching is off: add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY to the app's secrets.")
    elif rfq:
        matched = rfq.get("sure", 0) + rfq.get("likely", 0)
        notes.append(f"RFQs: {rfq.get('rfqs', 0):,} seen, {matched:,} matched to bets ({rfq.get('sure', 0):,} sure), "
                     f"{rfq.get('codes', 0)} codes, {rfq.get('linked', 0)} named. Stream {rfq.get('stream')}.")
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
            "users with the requester's code (a hash that stays the same for that person). A combo trade on the same "
            "market, with the same size, seconds later is that request filling, so it's that person's bet. **Sure** "
            "means only one request and one trade fit; otherwise the closest in time wins. When a matched trade shows "
            "a public nickname in the social feed, the code is linked to that name. Codes only build up while the app "
            "runs (Kalshi doesn't keep RFQ history), and need an API key in the app's secrets.\n"
            "- Kalshi's main trade feed has no trader ID. Its **social API** (what kalshi.com profiles use, free "
            "and keyless) returns the same trades with the maker's and taker's nicknames, but only for profiles "
            "that show their trades. A profile that hides them shows **hidden**, with only Kalshi's own PnL and "
            "markets-traded counts.\n"
            "- **PNL** is Kalshi's number for the profile (all-time, and 30D underneath). **TRADES** counts the fills "
            "in our sample (their last 1,000 trades within 30 days); **MKTS** is Kalshi's all-time markets traded.\n"
            "- **EV** prices every trade against the market's bid/ask mid in the minute it happened (one-minute "
            "candles), after taker fees. A combo is priced from its legs' books. A market maker earns the spread, "
            "so EV is positive; a taker who crosses the spread starts negative.\n"
            "- **BEAT** = settled PnL − EV on the same trades, and **Z** = that ÷ its standard deviation. Z tells "
            "luck from skill: around 0 is what the book expected, 2+ over hundreds of trades is rare by chance. "
            "That's the tennis-sharp signal: PnL far above what the book said their bets were worth.\n"
            "- **SCOPE** switches the sample between tennis-only and all of the bettor's trades. Tap a row for "
            "their latest trades. Maker fees (some markets charge them) aren't included.\n"
            "- The tracker refreshes every 15 minutes in the background; the first pass after a restart takes "
            "several minutes."
        )


PAGES = [st.Page(combo_page, title="Combo EV", icon="🎯", default=True),
         st.Page(bettors_page, title="Bettors", icon="📈", url_path="bettors")]
st.navigation(PAGES, position="hidden").run()
