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

from combo_tracker.client import KalshiClient  # noqa: E402  (after the reload check)
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.demo import DemoClient  # noqa: E402
from combo_tracker.pipeline import Board, Scorer  # noqa: E402
from combo_tracker.view import LIVE_HOURS, num, pack, slim_legs  # noqa: E402

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
    </style>"""
)

UI = Path(__file__).parent / "combo_tracker" / "ui"
combo_table = st.components.v2.component(
    "combo_table", css=(UI / "table.css").read_text(), js=(UI / "table.js").read_text())


def _stop_collectors() -> None:
    """Stop collectors left running by an earlier version of the app, so only one polls Kalshi."""
    for thread in threading.enumerate():
        if thread.name == "combo-collector" and hasattr(thread, "stop"):
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
        "Kalshi's public data has no bettor ID."
    )
