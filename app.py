"""Kalshi combo EV tracker: a phone-first Streamlit dashboard.

Run locally:  streamlit run app.py
Offline demo: DEMO=1 streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from combo_tracker.client import KalshiClient
from combo_tracker.demo import DemoClient
from combo_tracker.format import bet_card, fmt_odds, units
from combo_tracker.collector import Collector
from combo_tracker.pipeline import Scorer

DEMO = os.environ.get("DEMO") == "1"
REFRESH_SECONDS = 60
MAX_BETS = 400

SIZE_CHIPS = {"All": 0, "$10+": 10, "$100+": 100, "$1k+": 1000}
LOOKBACKS = {"15m": 15, "1h": 60, "3h": 180}
SORTS = {
    "EV % (mid)": "ev_pct_mid",
    "EV % (CRX)": "ev_pct_crx",
    "Kelly units": "kelly_mid",
    "Net $ EV": "ev_net",
    "Biggest bets": "stake",
    "Newest": "minutes_ago",
}
KELLY = {"Full": 1.0, "½": 0.5, "¼": 0.25, "⅛": 0.125}
# One-tap filters. Add new ones here and handle them in apply_quick_filters().
QUICK = ["+EV", "Clean", "No SG", "No thin", "YES only"]
TABLE_ROWS = 11  # rows visible before the table scrolls; ~10 bets per phone screen

st.set_page_config(page_title="Combo EV", page_icon="🎯", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """<style>
    header[data-testid="stHeader"] {display: none;}
    .block-container {padding-top: 1.6rem; padding-left: 0.6rem; padding-right: 0.6rem;}
    h1 {font-size: 1.25rem !important; padding: 0 0 0.2rem 0 !important; margin: 0 !important; line-height: 1.4 !important;}
    [data-testid="stVerticalBlock"] {gap: 0.5rem;}
    [data-testid="stMetricValue"] {font-size: 1.25rem !important;}
    [data-testid="stMetricDelta"] {font-size: 0.8rem !important;}
    [data-testid="stMetric"] {min-width: 4.5rem;}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_source():
    """One client, scorer and background collector shared by every visitor."""
    if DEMO:
        client = DemoClient()
        return client, Scorer(client), None
    client = KalshiClient()
    scorer = Scorer(client)
    # Score the default view after every poll so leg quotes and titles are cached before anyone looks.
    warm = lambda col: scorer.score(col.window(60), min_stake=100, max_bets=MAX_BETS)  # noqa: E731
    return client, scorer, Collector(client, max_window_minutes=max(LOOKBACKS.values()), after_poll=warm).start()


def window_trades(lookback: int) -> list[dict]:
    client, _, collector = get_source()
    if collector is None:
        return client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=lookback))
    return collector.window(lookback)


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def load(lookback: int, min_stake: float, group: bool):
    return get_source()[1].score(window_trades(lookback), min_stake=min_stake, group=group, max_bets=MAX_BETS)


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def with_depth(row: dict, lookback: int, group: bool) -> dict:
    """Re-score one bet against the full orderbook of each leg (list rows use top of book)."""
    return get_source()[1].rescore_with_depth(row, window_trades(lookback), group=group)


def flags(row: pd.Series) -> str:
    out = []
    if row["same_game_legs"]:
        out.append("SG")
    if row["min_depth_ratio"] is not None and row["min_depth_ratio"] < 1:
        out.append("THIN")
    if row["missing_quotes"] or row["inactive_legs"]:
        out.append("NO QUOTE")
    if row["outside_band"] is False:
        out.append("IN BAND")
    if row["ev_pct_mid"] is not None and row["ev_pct_mid"] > 2:
        out.append("CHECK")  # +200% edges are usually a stale book or a misread side
    hrs = row["hours_to_resolve"]
    if hrs is not None and hrs < 3:
        out.append(f"{max(hrs, 0):.0f}h" if hrs >= 1 else "LIVE?")
    return " · ".join(out)


def is_clean(row: pd.Series) -> bool:
    """Positive edge we would actually trust: outside the band and not flagged."""
    return bool(row["outside_band"]) and not row["same_game_legs"] and not row["missing_quotes"] \
        and (row["min_depth_ratio"] or 0) >= 1


def ago(dt: datetime) -> str:
    mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    return "just now" if mins < 1 else f"{mins:.0f} min ago"


# ---------- Controls ----------
st.title("🎯 Combo EV" + (" · demo" if DEMO else ""))

size_label = st.segmented_control("Min bet", list(SIZE_CHIPS), default="$100+", required=True,
                                  key="size", width="stretch", label_visibility="collapsed")
quick = st.pills("Quick filters", QUICK, selection_mode="multi", default=["+EV"], key="quick",
                 label_visibility="collapsed",
                 help="+EV: positive EV at mid · Clean: outside the leg band, deep books, no same-game legs · "
                      "No SG: hide same-game combos · No thin: hide combos with a shallow leg book")

bar = st.container(horizontal=True, vertical_alignment="center", gap="small")
with bar.popover("⚙️ More filters", width="stretch"):
    sort_label = st.selectbox("Default sort", list(SORTS), key="sort",
                              help="You can also tap any column header in the table to sort by it.")
    lookback_label = st.segmented_control("Bets placed in the last", list(LOOKBACKS), default="1h",
                                          required=True, key="lookback")
    custom_min = st.number_input("Custom min bet ($)", min_value=0, value=0, step=25,
                                 help="Overrides the size chips when greater than 0.")
    max_legs = st.slider("Max legs", 2, 12, 12)
    group = st.toggle("Merge fills of the same combo", value=True,
                      help="Kalshi often fills one bet in several pieces. Merged rows show the total size and average price.")
    st.markdown("**Kelly sizing**")
    kelly_label = st.segmented_control("Kelly fraction", list(KELLY), default="¼", required=True, key="kelly")
    unit_pct = st.number_input("1 unit = % of bankroll", min_value=0.1, max_value=100.0, value=1.0, step=0.5)
refresh_clicked = bar.button("↻", help="Refresh now", width="content")

min_stake = float(custom_min) if custom_min > 0 else float(SIZE_CHIPS[size_label])
lookback = LOOKBACKS[lookback_label]
kelly_mult = KELLY[kelly_label]

# ---------- Data ----------
if refresh_clicked:
    load.clear()
    with_depth.clear()
try:
    with st.spinner("Pulling combo trades and leg prices…"):
        snap = load(lookback, min_stake, group)
except Exception as exc:  # network / API errors should not blank the page
    st.error(f"Couldn't reach Kalshi: {exc}")
    st.stop()

collector = get_source()[2]
if collector is not None:
    covered = collector.coverage_minutes()
    if covered < lookback - 1:
        st.caption(f"⏳ Still loading history: {covered:.0f} of {lookback} min so far. Tap ↻ in a minute.")
    if collector.last_error:
        st.caption(f"⚠️ Last Kalshi poll failed, retrying: {collector.last_error[:150]}")

if not snap.rows:
    st.caption(f"Updated {snap.fetched_at:%H:%M} UTC ({ago(snap.fetched_at)})")
    st.info("No combo bets match these filters yet. Try a smaller minimum bet or a longer window.")
    st.stop()

df = pd.DataFrame(snap.rows)
df["label"] = df["leg_detail"].map(
    lambda legs: " · ".join(("NO " if l["side"] == "no" else "") + l["leg"] for l in legs))
df["flags"] = df.apply(flags, axis=1)
df["clean"] = df.apply(is_clean, axis=1)


def apply_quick_filters(view: pd.DataFrame) -> pd.DataFrame:
    if "+EV" in quick:
        view = view[view["ev_pct_mid"].fillna(-1) > 0]
    if "Clean" in quick:
        view = view[view["clean"]]
    if "No SG" in quick:
        view = view[view["same_game_legs"] == 0]
    if "No thin" in quick:
        view = view[view["min_depth_ratio"].fillna(0) >= 1]
    if "YES only" in quick:
        view = view[view["side"] == "yes"]
    return view


view = apply_quick_filters(df)
view = view[view["legs"] <= max_legs]
sort_col = SORTS[sort_label]
view = view.sort_values(sort_col, ascending=(sort_col == "minutes_ago"), na_position="last").reset_index(drop=True)

st.caption(
    f"**{len(view):,}** bets · {(df['ev_pct_mid'] > 0).sum():,} +EV of {len(df):,} ≥ \\${min_stake:,.0f} "
    f"in the last {lookback_label}"
    + (f" (largest {snap.bets_scored:,} of {snap.bets_over_min:,})" if snap.bets_over_min > MAX_BETS else "")
    + f" · updated {ago(snap.fetched_at)} · tap a row for details"
)

if view.empty:
    st.info("Nothing left after these filters. Turn some off above or under ⚙️ More filters.")
    st.stop()


def cents(x) -> str:
    return "–" if x is None or pd.isna(x) else f"{x * 100:.1f}¢"


def money(x, signed: bool = False) -> str:
    """Dollar string with the $ escaped, since Streamlit markdown reads $...$ as LaTeX."""
    if x is None or pd.isna(x):
        return "–"
    return f"{'+' if signed and x > 0 else ''}{'-' if x < 0 else ''}\\${abs(x):,.2f}"


def render_detail(r: pd.Series) -> None:
    st.code(bet_card(r.to_dict(), kelly_mult, unit_pct), language=None, wrap_lines=True)
    m = st.container(horizontal=True)
    m.metric("Paid", cents(r["price"]), help=f"{r['contracts']:,.0f} contracts over {r['fills']} fill(s)")
    m.metric("Fair (mid)", cents(r["fair_mid"]),
             delta=None if pd.isna(r["edge_mid"]) else f"{r['edge_mid'] * 100:+.1f}¢")
    m.metric("Fair (CRX)", cents(r["fair_crx"]),
             delta=None if pd.isna(r["fair_crx"]) else f"{(r['fair_crx'] - r['price']) * 100:+.1f}¢",
             help="Each leg priced by crossing the spread to the other side (its bid)")
    m.metric("CRX at size", cents(r["fair_at_size"]),
             delta=None if pd.isna(r["edge_at_size"]) else f"{r['edge_at_size'] * 100:+.1f}¢",
             help="CRX fair after walking each leg's book for the bet's contract count, so thin books count against it")
    hrs = r["hours_to_resolve"]
    roi = "–" if pd.isna(r["roi_net"]) else f"{r['roi_net'] * 100:+.0f}%"
    resolves = "–" if hrs is None or pd.isna(hrs) else f"{max(hrs, 0):.0f}h"
    st.caption(
        f"Net EV {money(r['ev_net'], signed=True)} = {money(r['ev_mid'], signed=True)} edge − {money(r['fee'])} fee · "
        f"ROI {roi}  \n"
        f"Leg band {cents(r['fair_bid'])}–{cents(r['fair_ask'])} (bids to asks) · weakest leg: {r['weakest_leg']}  \n"
        f"Placed {r['minutes_ago']:.0f} min ago · resolves in {resolves} · fair prices use the book now"
    )
    lines = []
    for leg in r["leg_detail"]:
        depth = leg["depth_ratio"]
        thin = depth is not None and depth < 1
        depth_txt = "–" if depth is None else f"{depth:.1f}×"
        lines.append(
            f"- **{leg['leg']}** {leg['side'].upper()} · {cents(leg['bid'])} / {cents(leg['ask'])}"
            f" · depth {':orange[' + depth_txt + ']' if thin else depth_txt}"
        )
    st.markdown("**Legs** (bid / ask · depth within 2¢ ÷ bet size)\n" + "\n".join(lines))
    st.caption(f"`{r['ticker']}`")


# ---------- Results ----------
@st.dialog("Bet details", width="large", on_dismiss=lambda: st.session_state.update(dismissed=st.session_state.get("open_bet")))
def show_detail(r: pd.Series) -> None:
    with st.spinner("Loading full leg orderbooks…"):
        try:
            deep = pd.Series(with_depth(r.drop(["label", "flags", "clean"]).to_dict(), lookback, group))
            deep["flags"], deep["clean"] = flags(deep), is_clean(deep)
        except Exception as exc:
            st.caption(f"Couldn't load full depth ({exc}); showing top of book.")
            deep = r
    render_detail(deep)


def ev_colors(col: pd.Series) -> list[str]:
    out = []
    for ev, clean in zip(view["ev_pct_mid"] if col.name == "EV" else view["ev_pct_crx"], view["clean"]):
        if ev is None or pd.isna(ev) or ev <= 0:
            out.append("color: #c0392b")
        elif clean and col.name == "EV":
            out.append("color: #1e8449; font-weight: 600")
        else:
            out.append("color: #b9770e; font-weight: 600" if col.name == "EV" else "")
    return out


table = pd.DataFrame({
    "#": range(1, len(view) + 1),
    "EV": view["ev_pct_mid"] * 100,
    "CRX": view["ev_pct_crx"] * 100,
    "u": view["kelly_mid"].map(lambda k: units(k, kelly_mult, unit_pct)),
    "Odds": view["price"].map(fmt_odds),
    "Bet": view["stake"],
    "Legs": view["side"].str.upper() + " " + view["legs"].astype(str) + "L · " + view["label"],
    "Flags": view["flags"],
    "Age": view["minutes_ago"],
})
event = st.dataframe(
    table.style.apply(ev_colors, subset=["EV", "CRX"]),
    hide_index=True,
    width="stretch",
    height=38 + 35 * min(len(table), TABLE_ROWS),
    row_height=35,
    on_select="rerun",
    selection_mode="single-cell",  # tap any cell to open the bet (no checkbox column)
    key="bets",
    column_config={
        "#": st.column_config.NumberColumn("#", width=30, pinned=True, help="Rank in the default sort"),
        "EV": st.column_config.NumberColumn("EV", format="%+.1f%%", width=64, pinned=True,
                                            help="EV at leg mids: fair ÷ price − 1, before fees"),
        "CRX": st.column_config.NumberColumn("CRX", format="%+.1f%%", width=62,
                                             help="EV with each leg priced by crossing the spread (its bid)"),
        "u": st.column_config.NumberColumn("u", format="%d", width=32, help="Kelly units at mid"),
        "Odds": st.column_config.TextColumn("Odds", width=56, help="American odds the bettor got"),
        "Bet": st.column_config.NumberColumn("Bet", format="$%.0f", width=62, help="Contracts × price paid"),
        "Legs": st.column_config.TextColumn("Legs", width=320, help="Bettor side, leg count, then each leg"),
        "Flags": st.column_config.TextColumn("Flags", width=120, help="SG same-game · THIN shallow book · "
                                             "IN BAND edge within leg spreads · NO QUOTE · CHECK edge over +200%"),
        "Age": st.column_config.NumberColumn("Age", format="%.0fm", width=48, help="Minutes since the bet"),
    },
)
# Tapping a column header to sort also focuses that row's first cell, so taps on
# the "#" column are ignored rather than opening a bet.
selected = [c for c in (event.selection.cells if event and event.selection else []) if c[1] != "#"]
if selected:
    r = view.iloc[selected[0][0]]
    st.session_state["open_bet"] = f"{r['ticker']}|{r['side']}"
    if st.session_state.get("dismissed") != st.session_state["open_bet"]:
        show_detail(r)
else:
    st.session_state["dismissed"] = None

with st.expander("ℹ️ How EV and flags work"):
    st.markdown(
        "- **Fair (mid)** multiplies each leg's mid price. That assumes the legs are independent.\n"
        "- **Fair (CRX)** prices each leg by crossing the spread to the other side, i.e. at its bid. "
        "It's the conservative number.\n"
        "- **EV %** = fair ÷ price paid − 1, before fees. Odds are American.\n"
        "- **Units** = Kelly fraction (p − price) ÷ (1 − price), times your Kelly multiplier, "
        "divided by your unit size. The defaults are ¼ Kelly and 1u = 1% of bankroll.\n"
        "- **Net EV \\$** = (fair − price paid) × contracts − Kalshi's taker fee (0.07 × C × P × (1−P), rounded up).\n"
        "- :green[**Green**] = positive and clean. :orange[**Orange**] = positive but flagged. :red[**Red**] = negative.\n"
        "- **CHECK**: EV over +200%. That's usually a stale book or a misread side, so verify it on Kalshi.\n"
        "- **SG**: same-game legs. They're correlated, so the true odds are usually better than the product.\n"
        "- **THIN**: a leg has less depth within 2¢ of its ask than the bet size.\n"
        "- **IN BAND**: the price sits between the product of leg bids and the product of leg asks, "
        "so the edge is within the noise of the leg spreads.\n"
        "- **NO QUOTE**: a leg has no two-sided market or is no longer active.\n"
        "- Fair prices use the order book **now**, not when the bet was placed, so they answer "
        "\"is this bet still good to copy?\".\n"
        "- Kalshi's public data has no bettor ID, so bets can't be tied to a person."
    )
