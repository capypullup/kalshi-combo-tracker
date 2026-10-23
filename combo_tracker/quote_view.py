"""The QUOTE page: is this combo's price still live, and what would makers quote me now?

Pick a recent bet (or paste a combo ticker). CHECK reads the combo's own orderbook,
its newest fills and its legs' books; WATCH repeats that every few seconds. GET QUOTE
sends a real RFQ with your API key and lists the makers' quotes with their EV. It
never accepts a quote.
"""

from __future__ import annotations

import html
import time
from datetime import datetime, timezone

import streamlit as st

from .quote import DemoRfqDesk, LiveCheck, RfqDesk, RfqError, cents, check, odds, quote_for
from .view import leg_live

WATCH_SECONDS = 5
CHECK_TTL = 20  # seconds a check is reused when nothing changed
RFQ_SECONDS = 45  # an RFQ is cancelled after this long
POLL_SECONDS = 2
DOLLARS = [10, 25, 50, 100, 250, 500]

CSS = """<style>
.qv {font-family: ui-sans-serif, -apple-system, "SF Pro Text", Inter, system-ui, sans-serif; color: #e6fbff;
     font-variant-numeric: tabular-nums; font-size: 13px; line-height: 1.3;}
.qv .verdict {border: 1px solid #1b3540; border-radius: 10px; padding: 8px 10px; margin: 2px 0 6px;}
.qv .verdict b.word {font-size: 18px; letter-spacing: 0.12em; margin-right: 8px;}
.qv .LIVE {border-color: #2ee6a6;} .qv .LIVE b.word {color: #2ee6a6;}
.qv .LIKELY {border-color: #00e5ff;} .qv .LIKELY b.word {color: #00e5ff;}
.qv .MOVED b.word, .qv .STALE b.word, .qv .NODATA b.word {color: #ffc857;}
.qv .dim {color: #6f8f99;} .qv .pos {color: #2ee6a6;} .qv .neg {color: #ff5c6c;} .qv .cy {color: #00e5ff;}
.qv .warn {color: #ffc857;}
.qv h4 {font-size: 11px; letter-spacing: 0.12em; color: #6f8f99; margin: 10px 0 3px; font-weight: 700;}
.qv table {width: 100%; border-collapse: collapse;}
.qv td, .qv th {padding: 4px 4px; border-bottom: 1px solid #12222a; text-align: right; vertical-align: top;}
.qv th {font-size: 10px; letter-spacing: 0.1em; color: #6f8f99; font-weight: 700;}
.qv td:first-child, .qv th:first-child {text-align: left;}
.qv td small {display: block; color: #6f8f99; font-size: 11px;}
.qv .chips span {display: inline-block; border: 1px solid #1b3540; border-radius: 8px; padding: 1px 7px;
     margin: 0 4px 4px 0; font-size: 12px;}
.qv .ld {display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #ff5c6c; margin-right: 4px;}
</style>"""


def _c(p: float | None, digits: int | None = None) -> str:
    if p is None:
        return "–"
    return cents(p) if digits is None else f"{p * 100:.{digits}f}¢"


def _pct(x: float | None) -> str:
    if x is None:
        return '<span class="dim">–</span>'
    return f'<span class="{"pos" if x > 0 else "neg"}">{x * 100:+.1f}%</span>'


def _ago(ts: datetime, now: datetime) -> str:
    s = max(0, int((now - ts).total_seconds()))
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def _bet_label(r: dict) -> str:
    t = r["last_time"].astimezone().strftime("%H:%M")
    ev = r.get("ev_pct_mid")
    ev = f" · EV {ev * 100:+.0f}%" if ev is not None else ""
    return f"{t} · {r['side'].upper()} {odds(r['price'])} · ${r['stake']:,.0f} · {r['legs']} legs{ev}"


def _verdict_html(c: LiveCheck) -> str:
    now = c.checked_at
    s = c.scored
    word = c.verdict.replace(" ", "")
    out = [f'<div class="verdict {word}"><b class="word">{html.escape(c.verdict)}</b>{html.escape(c.reason)}<br>']
    if c.best_now is not None:
        out.append(f'<span class="dim">Best now</span> <b>{_c(c.best_now)}</b> {odds(c.best_now)} · '
                   f'EV MID {_pct(c.ev_now.get("mid"))} · CROSS {_pct(c.ev_now.get("cross"))}<br>')
    out.append(f'<span class="dim">At {_c(c.price)} ({odds(c.price)}):</span> EV MID {_pct(s["ev_pct_mid"])} · '
               f'CROSS {_pct(s["ev_pct_crx"])} <span class="dim">· fair {_c(s["fair_mid"])} / {_c(s["fair_crx"])} · '
               f'checked {now.astimezone().strftime("%H:%M:%S")}</span></div>')

    out.append(f"<h4>COMBO BOOK · BUY {c.side.upper()}</h4>")
    if c.book_levels:
        out.append('<div class="chips">' + "".join(
            f'<span class="{"pos" if p <= c.price + 1e-9 else ""}">{_c(p, 2)} × {n:,.0f}</span>'
            for p, n in c.book_levels) + "</div>")
    else:
        out.append('<div class="dim">No resting orders. Makers only answer RFQs on this one.</div>')

    fills = c.fills[:12]
    out.append(f"<h4>NEWEST FILLS · {len(c.side_fills)} ON {c.side.upper()} IN THE LAST {len(c.fills)} TRADES</h4>")
    if fills:
        rows = "".join(
            f'<tr><td>{_ago(f.ts, now)} ago</td><td class="{"cy" if f.side == c.side else "dim"}">{f.side.upper()}</td>'
            f'<td class="{"pos" if f.side == c.side and f.price <= c.price * 1.02 else ""}">{_c(f.price, 2)}</td>'
            f"<td>{f.contracts:,.0f}</td><td>${f.stake:,.2f}</td></tr>" for f in fills)
        out.append(f"<table><tr><th>WHEN</th><th>SIDE</th><th>PRICE</th><th>CONTRACTS</th><th>$</th></tr>{rows}</table>")
    else:
        out.append('<div class="dim">No fills on this combo.</div>')

    legs = s["leg_detail"]
    out.append(f"<h4>LEGS · {len(legs)}</h4><table><tr><th>LEG</th><th>BID</th><th>ASK</th><th>TOP</th></tr>")
    for l in legs:
        live = '<span class="ld"></span>' if leg_live(l) else ""
        thin = (l["spread"] or 1) > 0.03 or min(l["top_bid_size"], l["top_ask_size"]) < 50
        out.append(
            f'<tr><td>{live}<span class="cy">{l["side"].upper()}</span> {html.escape(l["leg"])}'
            f'<small>{html.escape(l["event_title"])}</small></td><td>{_c(l["bid"])}</td><td>{_c(l["ask"])}</td>'
            f'<td class="{"warn" if thin else ""}">{min(l["top_bid_size"], l["top_ask_size"]):,.0f}</td></tr>')
    out.append("</table>")
    return '<div class="qv">' + "".join(out) + "</div>"


def _quotes_html(raw: list[dict], c: LiveCheck) -> str:
    quotes = [q for q in (quote_for(r, c.side) for r in raw) if q and q.status in ("open", "")]
    if not quotes:
        return '<div class="qv dim">Waiting for makers…</div>'
    quotes.sort(key=lambda q: q.all_in)
    fm, fc = c.scored["fair_mid"], c.scored["fair_crx"]
    rows = "".join(
        f"<tr><td>{_c(q.all_in)}<small>{odds(q.all_in)}</small></td><td>{_c(q.price)}<small>{odds(q.price)}</small></td>"
        f"<td>{q.contracts:,.2f}<small>{html.escape(q.maker)}</small></td>"
        f"<td>{_pct(fm / q.all_in - 1 if fm else None)}</td><td>{_pct(fc / q.all_in - 1 if fc else None)}</td></tr>"
        for q in quotes)
    return (f'<div class="qv"><table><tr><th>+FEE</th><th>BUY {c.side.upper()}</th><th>SIZE</th><th>EV MID</th>'
            f"<th>EV CROSS</th></tr>{rows}</table></div>")


def _pick_from_query() -> dict | None:
    q = st.query_params
    if not q.get("t"):
        return None
    try:
        return {"ticker": q["t"], "side": q.get("side", "yes"), "price": float(q.get("p", 0)) or None,
                "contracts": float(q.get("n", 0)) or None}
    except ValueError:
        return {"ticker": q["t"], "side": q.get("side", "yes"), "price": None, "contracts": None}


def render(client, rows: list[dict], starts=None, auth=None, demo: bool = False) -> None:
    """The page body. `rows` are scored recent bets to pick from, newest first."""
    st.html(CSS)
    ss = st.session_state
    picked = _pick_from_query()

    options = {f"{r['ticker']}|{r['side']}": r for r in rows[:200]}
    keys = ["paste"] + list(options)
    want = f"{picked['ticker']}|{picked['side']}" if picked else None
    index = keys.index(want) if want in options else 0 if picked or len(keys) == 1 else 1
    choice = st.selectbox("Recent bet", keys, key="q_pick", index=index,
                          format_func=lambda k: "Paste a combo ticker…" if k == "paste" else _bet_label(options[k]),
                          label_visibility="collapsed")
    if choice == "paste":
        ticker = st.text_input("Combo ticker", value=picked["ticker"] if picked else "", key="q_ticker",
                               placeholder="KXMVECROSSCATEGORY-…").strip().upper()
        base = {"side": picked["side"] if picked else "yes", "price": picked and picked["price"],
                "contracts": picked and picked["contracts"], "since": None}
    else:
        r = options[choice]
        ticker, base = r["ticker"], {"side": r["side"], "price": r["price"], "contracts": r["contracts"],
                                     "since": r["last_time"]}
    if not ticker:
        st.caption("Pick a bet from the list, or paste a combo ticker (it starts with KXMVE).")
        return

    with st.container(horizontal=True, vertical_alignment="bottom"):
        side = st.segmented_control("Side", ["yes", "no"], default=base["side"], key=f"q_side_{ticker}",
                                    format_func=str.upper) or base["side"]
        price = st.number_input("Price ¢", min_value=0.01, max_value=99.99, step=0.1, format="%.2f",
                                key=f"q_price_{ticker}_{side}",
                                value=round((base["price"] or 0.5) * 100, 2) if side == base["side"] else 50.0) / 100
        contracts = base["contracts"] or 100.0
        force = st.button("CHECK", type="primary")
        watch = st.toggle("WATCH", key="q_watch", help=f"Re-check every {WATCH_SECONDS}s")

    def live_part():
        key = (ticker, side, round(price, 4))
        last = ss.get("q_check")
        if force or watch or not last or last["key"] != key or time.time() - last["at"] > CHECK_TTL:
            try:
                last = {"key": key, "at": time.time(), "check": check(client, ticker, side, price, contracts, starts,
                                                                        since=base["since"] if side == base["side"] else None,
                                                                        cache=ss.setdefault("q_legs", {}))}
            except Exception as exc:  # network / unknown ticker
                st.error(f"Couldn't check {ticker}: {exc}"[:300])
                return
            ss.q_check = last
        st.html(_verdict_html(last["check"]))

    st.fragment(live_part, run_every=WATCH_SECONDS if watch else None)()

    st.markdown("**GET QUOTE**  \n<span style='color:#6f8f99;font-size:12px'>Asks every maker for this combo at "
                "your size, like the sportsbook button. Nothing is bought.</span>", unsafe_allow_html=True)
    desk = _desk(client, auth, demo, ticker)
    with st.container(horizontal=True, vertical_alignment="bottom"):
        dollars = st.pills("Size", DOLLARS, default=50, format_func=lambda d: f"${d}", key="q_dollars",
                           label_visibility="collapsed") or 50
        get = st.button("GET QUOTE", type="primary", disabled=desk is None)
    if desk is None:
        st.caption("Needs your Kalshi API key: add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY to the app's secrets "
                   "(Manage app → Settings → Secrets).")
        return
    if get:
        _cancel(desk)
        try:
            ss.q_rfq = {"id": desk.create(ticker, dollars), "at": time.time(), "ticker": ticker, "side": side,
                        "dollars": dollars, "quotes": []}
        except RfqError as exc:
            st.error(f"Kalshi refused the RFQ: {exc}"[:300])

    rfq = ss.get("q_rfq")
    if rfq and rfq["ticker"] == ticker:
        def quotes_part():
            rfq = ss.get("q_rfq")
            if not rfq:
                return
            age = time.time() - rfq["at"]
            if age < RFQ_SECONDS:
                try:
                    rfq["quotes"] = desk.quotes(rfq["id"])
                except RfqError as exc:
                    st.error(str(exc)[:300])
            elif not rfq.get("closed"):
                _cancel(desk)
                rfq["closed"] = True
            c = ss.get("q_check", {}).get("check")
            status = (f"Open {age:.0f}s · ${rfq['dollars']}" if not rfq.get("closed")
                      else f"Closed after {RFQ_SECONDS}s (prices may have changed)")
            st.html(f'<div class="qv"><h4>RECEIVED QUOTES ({len(rfq["quotes"])}) · {status}</h4></div>')
            if c is not None and c.side == rfq["side"]:
                st.html(_quotes_html(rfq["quotes"], c))
            if not rfq.get("closed") and st.button("cancel RFQ", type="tertiary"):
                _cancel(desk)
                rfq["closed"] = True
                st.rerun(scope="fragment")

        st.fragment(quotes_part, run_every=None if rfq.get("closed") else POLL_SECONDS)()
        st.caption("Accepting a quote (placing the bet) isn't built yet: that's the copy-bet step.")


def _desk(client, auth, demo: bool, ticker: str):
    ss = st.session_state
    if demo:
        c = ss.get("q_check", {}).get("check")
        fair = 0.3
        if c and c.ticker == ticker and c.scored["fair_mid"]:  # fair_mid is for the side checked
            fair = c.scored["fair_mid"] if c.side == "yes" else 1 - c.scored["fair_mid"]
        desk = ss.setdefault("q_demo_desk", DemoRfqDesk({}))
        desk.fair[ticker] = fair
        return desk
    return RfqDesk(client, auth) if auth is not None else None


def _cancel(desk) -> None:
    rfq = st.session_state.get("q_rfq")
    if rfq and not rfq.get("closed"):
        try:
            desk.cancel(rfq["id"])
        except Exception:  # already closed or filled
            pass
        rfq["closed"] = True
