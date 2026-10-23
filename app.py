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
    .st-key-nav {gap: 5px; padding: 2px 0 2px;}
    .capy-brand {display: flex; align-items: center; gap: 5px;}
    .capy-brand img {width: 34px; height: 34px; flex: none;}
    .capy-brand .wm {font-weight: 900; font-size: 21px; letter-spacing: -0.01em; line-height: 1; color: #e8ae78;}
    .capy-brand .wm b {font-weight: 900; color: #00e5ff;}
    .st-key-nav [data-testid="stPageLink"] a {border: 1px solid #1b3540; border-radius: 999px; padding: 3px 11px;
        min-height: 0;}
    .st-key-nav [data-testid="stPageLink"] a span {color: #8fb0b9;}
    .st-key-nav [data-testid="stPageLink"] a p {font-size: 13px; font-weight: 700;}
    .st-key-nav [data-testid="stPageLink"] a[aria-current="page"] {border-color: #00e5ff; background: rgba(0, 229, 255, 0.14);}
    .st-key-nav [data-testid="stPageLink"] a[aria-current="page"] span {color: #00e5ff;}
    </style>"""
)

combo_table = st.components.v2.component(
    "combo_table", css=(UI / "table.css").read_text(),
    js=(UI / "table.js").read_text().replace('"__LOGO__"', json.dumps(LOGO)))
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
        st.switch_page(PAGES[2], query_params={"t": go["t"], "side": go["side"], "p": f"{go['p']:.4f}",
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
            error = error or f"Last Kalshi poll failed, retrying: {collector.last_error[:150]}"
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
    """The app bar (the Streamlit header is hidden): CapyEV on the left, the pages on the right."""
    with st.container(horizontal=True, key="nav", vertical_alignment="center"):
        st.html(f'<div class="capy-brand"><img src="{LOGO_URI}" alt=""><span class="wm">Capy<b>EV</b></span></div>'
                f"{TOUCH_ICON_JS}", width="content", unsafe_allow_javascript=True)
        st.space("stretch")
        st.page_link(PAGES[0], label="Combos")
        st.page_link(PAGES[1], label="Bettors")
        st.page_link(PAGES[2], label="Quote")
        st.page_link(PAGES[3], label="Tools")


def combo_page() -> None:
    nav()
    table_view()

    with st.expander("How EV, units and flags work"):
        st.markdown(
            "- **EV mid** multiplies each leg's mid price to get a fair price, then EV = fair ÷ price paid − 1. "
            "That assumes the legs are independent.\n"
            "- **EV cross** prices each leg by crossing the spread (its bid). It's the conservative number.\n"
            "- **Units** = Kelly fraction (p − price) ÷ (1 − price), times your Kelly multiplier, divided by your "
            "unit size. Set both with the gear icon. The defaults are ¼ Kelly and 1u = 1% of bankroll.\n"
            "- **Min EV / Min QK** filter on whichever basis the Mid / Cross switch shows. Tap a chip again to clear it.\n"
            "- **Indep. only** hides combos with two legs on the same game (they're correlated, so the product "
            "of leg prices understates the true odds). Those rows show their legs/games count in yellow.\n"
            "- **Pregame / Live** uses each game's scheduled start from Kalshi's schedule: a combo is live once "
            "any leg's game has started (red dot by the time; open a bet to see each leg's start). Legs Kalshi "
            f"doesn't schedule count as live within {LIVE_HOURS}h of their expected settlement.\n"
            "- **Hide thin** hides combos with a leg whose spread is wide or whose top of book is small, since "
            "its mid isn't a reliable price. Set the cutoffs with the gear icon; thin quotes show in yellow with *.\n"
            "- **Full book** (book icon on an open bet) walks each leg's whole orderbook for the bet's size.\n"
            "- **Bet in Kalshi** (on an open bet) opens that combo in the Kalshi app, or in the combo builder on "
            "kalshi.com if the app isn't installed. Kalshi quotes it fresh, so check the price still beats the fair "
            "odds before you buy. **Quote it** checks the live price on the Quote page instead.\n"
            "- **Flags**: THIN see Hide thin · IN BAND the price "
            "sits inside the leg spreads, so the edge is noise · NO QUOTE a leg has no market · CHECK an edge "
            "over +200%, usually a stale book.\n"
            "- **Mid odds / Cross odds** show the fair prices behind EV mid and EV cross as odds: the multiplier a "
            "fair bet would pay, with American odds under it. Compare them with the multiplier the bettor got.\n"
            "- **↻ Refresh** pulls the newest trades and leg quotes from Kalshi and re-scores this view, usually in "
            "10–15 seconds. The icon spins until the new bets are in; the list never changes on its own.\n"
            "- Fair prices use the order book **now**, so they answer \"is this still good to copy?\". "
            "Kalshi's trade feed has no bettor ID, so these bets are anonymous (the Bettors page covers public profiles)."
        )
    refresh_watch()


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
    quote_view.render(client, rows, starts=src.scorer.starts, auth=None if DEMO else _kalshi_auth()[0], demo=DEMO)
    with st.expander("How to read this"):
        st.markdown(
            "- A fill in the feed means someone got that price, not that you still can. This page checks one combo "
            "**now**.\n"
            "- **LIVE**: the combo's own orderbook has resting orders at or under the price, or it filled at that "
            "price in the last 90 seconds. **LIKELY**: its newest fill was at or under the price, but a few minutes "
            "ago. **MOVED**: newer fills are at a worse price. **STALE**: nothing on this side in 10 minutes.\n"
            "- **Combo book**: makers often leave orders resting on a combo after it trades. Green levels are at or "
            "under your price, so you could take them now with a limit order.\n"
            "- **EV** compares the price with the legs' books right now (mid, and crossing the spread). "
            "**Best now** is the lowest price that looks available: the book's ask or a fill in the last 90 seconds.\n"
            "- **Watch** re-checks every 5 seconds while it's on.\n"
            "- **Get quote** sends a real request for quote with your API key, for about the dollar amount you "
            "pick (fees included), and lists makers' quotes as they arrive with EV after your fee. It's cancelled "
            "after 45 seconds. Nothing is ever accepted from this page."
        )


# ---------- Tools page ----------
def tools_page() -> None:
    nav()
    tools_view.render()


PAGES = [st.Page(combo_page, title="Combos", default=True),
         st.Page(bettors_page, title="Bettors", url_path="bettors"),
         st.Page(quote_page, title="Quote", url_path="quote"),
         st.Page(tools_page, title="Tools", url_path="tools")]
st.navigation(PAGES, position="hidden").run()
