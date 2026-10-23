"""The Quote page: paste a combo, see whether its price is still there, get makers' quotes
at your size, and bet one.

1. Paste a ticker (or a CapyEV /quote link) with the big button, or pick a recent bet.
2. The live check reads the combo's own orderbook, its newest fills and its legs' books,
   and shows how the legs moved since CapyEV scored the bet.
3. Get quotes sends a real RFQ with your API key at your size (in units) and lists the
   makers' quotes with EV and Kelly units. Bet takes one after you confirm it.
4. Add legs suggests near-certain legs on other games: quote the bigger combo too and see
   whether makers price it better than the legs cost.

Betting needs the TRADE_PIN secret (the app is public), and quotes need it once it's set.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from .engine import _parse_time, legs_of
from .links import bet_url
from .quote import (KELLY, REAPER, UNIT_DOLLARS, DemoRfqDesk, LiveCheck, RfqDesk, RfqError, cents, check, explain,
                    extra_legs, kelly_stake, leg_moves, mult, odds, parse_paste, quote_for,
                    recent_trades)
from .view import leg_live

UI = Path(__file__).parent / "ui"
WATCH_SECONDS = 5
CHECK_TTL = 20  # seconds a check is reused when nothing changed
RFQ_SECONDS = 45  # an RFQ is cancelled after this long
POLL_SECONDS = 2
UNIT_PILLS = [0.1, 0.25, 0.5, 1.0, 2.0]
MAX_BET_UNITS = 3.0  # a safety cap on one bet from this page
ACCEPT_WAIT = 8  # seconds to wait for the maker to confirm and the trade to execute
PIN_COOKIE = "capyev_trade"
PIN_TRIES = 8  # wrong PINs allowed per hour, across everyone
_PIN_LOCK = threading.Lock()

CSS = """<style>
.qv {font-family: "Nunito", ui-rounded, "SF Pro Rounded", ui-sans-serif, -apple-system, system-ui, sans-serif; color: #e6fbff;
     font-variant-numeric: tabular-nums; font-size: 13px; line-height: 1.3;}
.qv .card {border: 1px solid #1b3540; border-radius: 16px; padding: 9px 12px; margin: 2px 0 6px;}
.qv .verdict b.word {font-size: 18px; font-weight: 900; letter-spacing: 0.06em; margin-right: 8px;}
.qv .LIVE {border-color: #2ee6a6;} .qv .LIVE b.word {color: #2ee6a6;}
.qv .LIKELY {border-color: #00e5ff;} .qv .LIKELY b.word {color: #00e5ff;}
.qv .MOVED b.word, .qv .STALE b.word, .qv .NODATA b.word {color: #ffc857;}
.qv .dim {color: #6f8f99;} .qv .pos {color: #2ee6a6;} .qv .neg {color: #ff5c6c;} .qv .cy {color: #00e5ff;}
.qv .warn {color: #ffc857;} .qv .capy {color: #e8ae78;}
.qv h4 {font-size: 12.5px; color: #6f8f99; margin: 10px 0 3px; font-weight: 800;}
.qv table {width: 100%; border-collapse: collapse;}
.qv td, .qv th {padding: 4px 4px; border-bottom: 1px solid #12222a; text-align: right; vertical-align: top;}
.qv th {font-size: 11.5px; color: #6f8f99; font-weight: 800;}
.qv td:first-child, .qv th:first-child {text-align: left;}
.qv td small {display: block; color: #6f8f99; font-size: 11px;}
.qv tr.best td {background: rgba(46, 230, 166, 0.08);}
.qv .chips span {display: inline-block; border: 1px solid #1b3540; border-radius: 999px; padding: 1px 8px;
     margin: 0 4px 4px 0; font-size: 12px;}
.qv .ld {display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #ff5c6c; margin-right: 4px;}
.qv .big {font-size: 17px; font-weight: 900;}
.qv .legs div {padding: 2px 0;}
.qv .tick {font-size: 11px; color: #6f8f99; word-break: break-all;}
.qv .sec {font-size: 17px; font-weight: 900; margin: 10px 0 0;}
</style>"""


# ---------- formatting ----------
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


def _clock(ts: datetime, fmt: str = "%H:%M") -> str:
    """A time in the phone's zone (the server runs on UTC)."""
    try:
        tz = ZoneInfo(st.context.timezone) if st.context.timezone else None
    except Exception:  # no browser zone yet, or an unknown one
        tz = None
    return ts.astimezone(tz).strftime(fmt) if tz else ts.astimezone(timezone.utc).strftime(fmt) + " UTC"


def _odds(p: float | None) -> str:
    """'8.33x <small>+733</small>': the Kalshi app's multiplier with American odds."""
    return f"{mult(p)}<small>{odds(p)}</small>"


def _units(dollars: float, unit: float) -> str:
    u = dollars / unit if unit else 0
    return f"{u:.2f}u" if u < 1 else f"{u:.1f}u"


def _bet_label(r: dict) -> str:
    t = _clock(r["last_time"])
    ev = r.get("ev_pct_mid")
    ev = f" · EV {ev * 100:+.0f}%" if ev is not None else ""
    return f"{t} · {r['side'].upper()} {mult(r['price'])} · ${r['stake']:,.0f} · {r['legs']} legs{ev}"


# ---------- the bet being looked at ----------
def _ctx_from(r: dict, scored_at: datetime | None) -> dict:
    return {"ticker": r["ticker"], "side": r["side"], "price": r["price"], "contracts": r["contracts"],
            "since": r["last_time"], "row": r, "seen_at": scored_at}


def _params(ctx: dict) -> dict:
    """The link for this bet (what Quote it on the Combos page sends)."""
    out = {"t": ctx["ticker"]}
    if ctx.get("side"):
        out["side"] = ctx["side"]
    if ctx.get("price"):
        out["p"] = f"{ctx['price']:.4f}"
    if ctx.get("contracts"):
        out["n"] = f"{ctx['contracts']:g}"
    return out


def _fresh(ctx: dict | None) -> None:
    """Start over on a new bet: drop the last one's card, result and picks, and redraw the side and price
    boxes (new keys: a box keeps what the phone shows otherwise, even for the same combo)."""
    ss = st.session_state
    ss.q_ctx, ss.q_extra = ctx, []
    for k in ("q_confirm", "q_bet", "q_auto"):
        ss.pop(k, None)
    ss.q_gen = ss.get("q_gen", 0) + 1


def _set_ctx(ctx: dict | None) -> None:
    _fresh(ctx)
    if ctx:
        st.query_params.from_dict(_params(ctx))
    else:
        st.query_params.clear()


def _from_paste(text: str, rows: list[dict], scored_at) -> dict | None:
    """The pasted combo, filled in from the newest recent bet on it when there is one."""
    got = parse_paste(text)
    if not got:
        return None
    mine = [r for r in rows if r["ticker"] == got["ticker"] and (got["side"] in (None, r["side"]))]
    if mine:
        ctx = _ctx_from(mine[0], scored_at)
        if got["price"]:
            ctx["price"] = got["price"]
        if got["contracts"]:
            ctx["contracts"] = got["contracts"]
        return ctx
    return {**got, "since": None, "row": None, "seen_at": None}


# ---------- betting lock ----------
def _token(pin: str, salt: str) -> str:
    return hmac.new(pin.encode(), f"capyev-trade:{salt}".encode(), hashlib.sha256).hexdigest()[:32]


@st.cache_resource
def _pin_misses() -> list[float]:
    return []  # times of wrong PINs, shared by every visitor


def _same(a, b: str) -> bool:
    return isinstance(a, str) and hmac.compare_digest(a.encode(), b.encode())


def _unlocked(pin: str | None, salt: str) -> bool:
    if not pin:
        return True
    ss = st.session_state
    token = _token(pin, salt)  # a new PIN (or key) locks every session that unlocked with the old one
    if not _same(ss.get("q_unlocked"), token):
        try:
            cookie = st.context.cookies.get(PIN_COOKIE)
        except Exception:  # no request context (tests)
            cookie = None
        if _same(cookie, token):
            ss.q_unlocked = token
    return _same(ss.get("q_unlocked"), token)


def _unlock_form(pin: str, salt: str) -> None:
    misses = _pin_misses()
    misses[:] = [t for t in misses if time.time() - t < 3600]
    if len(misses) >= PIN_TRIES:
        st.warning("Too many wrong PINs in the last hour. Quotes and betting are locked for a while.")
        return
    with st.form("q_pin", border=False):
        with st.container(horizontal=True, vertical_alignment="bottom"):
            typed = st.text_input("Trade PIN", type="password", placeholder="Your TRADE_PIN",
                                  label_visibility="collapsed")
            ok = st.form_submit_button("Unlock", type="primary")
    if ok:
        with _PIN_LOCK:  # sessions run in parallel threads: count every try
            misses[:] = [t for t in misses if time.time() - t < 3600]
            locked = len(misses) >= PIN_TRIES
            good = not locked and hmac.compare_digest(typed.strip().encode(), pin.encode())
            if not locked and not good:
                misses.append(time.time())
        if good:
            st.session_state.q_unlocked = _token(pin, salt)
            # Remember this phone for 30 days (the cookie holds a hash, not the PIN).
            st.session_state.q_cookie = (f"{PIN_COOKIE}={_token(pin, salt)}; Max-Age={30 * 86400}; Path=/; "
                                         "SameSite=Strict; Secure")
            st.rerun()
        st.error("Too many wrong PINs in the last hour." if locked else "Wrong PIN.")
    st.caption("Quotes and bets use your Kalshi key, so they need the TRADE_PIN from the app's secrets. "
               "This phone stays unlocked for 30 days.")


# ---------- page ----------
def render(client, rows: list[dict], starts=None, auth=None, demo: bool = False, pool=None,
           scored_at: datetime | None = None, pin: str | None = None, auth_problem: str | None = None) -> None:
    """The page body.

    `rows` are scored recent bets (newest first), `pool()` returns the leg markets and events
    CapyEV has cached (for extra legs), `pin` is TRADE_PIN.
    """
    st.html(CSS)
    ss = st.session_state
    ss.setdefault("q_unit", UNIT_DOLLARS)  # plain keys: the Sizing widgets aren't drawn on every run
    ss.setdefault("q_kelly", KELLY)

    q = st.query_params
    ctx = ss.get("q_ctx")
    if "q_ctx" not in ss or (q.get("t") and (ctx is None or q["t"].upper() != ctx["ticker"])):
        # First load, or the Combos page sent a bet (it drops q_ctx first): take it from the link.
        ctx = _from_paste(f"?t={q['t']}&side={q.get('side', '')}&p={q.get('p', '')}&n={q.get('n', '')}", rows,
                          scored_at) if q.get("t") else None
        _fresh(ctx)
        if ctx:  # the link as parsed, so a stray character in it doesn't reload the bet on every run
            st.query_params.from_dict(_params(ctx))
        elif q.get("t"):
            st.query_params.clear()  # not a combo link

    def on_paste():
        text = getattr(ss.q_paste, "text", None)
        new = _from_paste(text or "", rows, scored_at)
        if new is None:
            shown = (text or "")[:60].replace("`", "")
            ss.q_paste_err = f"That doesn't look like a combo ticker: `{shown}`"
        else:
            ss.pop("q_paste_err", None)
            _set_ctx(new)

    paste = st.components.v2.component("quote_paste", css=(UI / "paste.css").read_text(),
                                       js=(UI / "paste.js").read_text())
    paste(key="q_paste", data={"small": bool(ctx), "label": "Paste another" if ctx else "Paste ticker",
                               "cookie": ss.pop("q_cookie", None)}, on_text_change=on_paste)
    if ss.get("q_paste_err"):
        st.warning(ss.q_paste_err)

    desk = _desk(client, auth, demo, ctx["ticker"] if ctx else "")
    _close_others(desk, ctx["ticker"] if ctx else None)
    if ctx is None:
        _landing(rows, scored_at)
        return

    salt = auth.key_id if auth is not None else "demo"
    unlocked = _unlocked(pin, salt)

    _header(ctx)
    if not ctx.get("price"):
        _guess_price(client, ctx)
        # The side, not the guessed price: a reload guesses again, so that fill still doesn't count as proof.
        st.query_params.from_dict({k: v for k, v in _params(ctx).items() if k != "p"})

    with st.container(horizontal=True, vertical_alignment="bottom"):
        gen = ss.get("q_gen", 0)
        side = st.segmented_control("Side", ["yes", "no"], default=ctx["side"] or "yes",
                                    key=f"q_side_{ctx['ticker']}_{gen}", format_func=str.upper) or ctx["side"] or "yes"
        same = side == (ctx["side"] or "yes")
        price = st.number_input("Price ¢", min_value=0.01, max_value=99.99, step=0.1, format="%.2f",
                                key=f"q_price_{ctx['ticker']}_{side}_{gen}",
                                value=min(99.99, max(0.01, round((ctx["price"] or 0.5) * 100, 2))) if same else 50.0) / 100
        force = st.button("Check", type="primary")
        watch = st.toggle("Watch", key="q_watch", help=f"Re-check every {WATCH_SECONDS}s")
    st.caption(f"{side.upper()} at {mult(price)} ({odds(price)})" + (
        f" · bet at {_clock(ctx['since'], '%H:%M:%S')}" if ctx.get("since") and same else ""))

    def live_part():
        key = (ctx["ticker"], side, round(price, 4))
        last = ss.get("q_check")
        if force or watch or not last or last["key"] != key or time.time() - last["at"] > CHECK_TTL:
            try:
                last = {"key": key, "at": time.time(), "check": check(
                    client, ctx["ticker"], side, price, ctx.get("contracts") or 100.0, starts,
                    since=ctx.get("since") if same else None, cache=ss.setdefault("q_legs", {}))}
            except Exception as exc:  # network / unknown ticker
                st.error(f"Couldn't check {ctx['ticker']}: {exc}"[:300])
                return
            ss.q_check = last
        c = last["check"]
        st.html(_verdict_html(c, ctx.get("row") if same else None, ctx.get("seen_at")))

    st.fragment(live_part, run_every=WATCH_SECONDS if watch else None)()

    c = (ss.get("q_check") or {}).get("check")
    if c is None or c.ticker != ctx["ticker"] or c.side != side:  # the check failed: never price with the other side's
        return

    st.html('<div class="qv"><div class="sec">Quotes</div></div>')
    if desk is None:
        st.info(auth_problem or "Quotes need your Kalshi API key: add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY to the "
                "app's secrets (Manage app → Settings → Secrets).")
    elif not unlocked:
        _unlock_form(pin, salt)
    else:
        _quotes_section(desk, c, side, bool(pin) or demo)

    _open_in_kalshi(c)
    if side == "yes":
        _extra_legs_section(desk if unlocked else None, client, c, pool, starts)
    _settings()
    _key_section(desk if unlocked else None)


def _landing(rows: list[dict], scored_at) -> None:
    st.html('<div class="qv"><div class="dim" style="text-align:center;padding:2px 0 8px">On the Combos page, open a bet '
            "and tap Quote it, or copy its ticker and tap Paste here. Or pick a recent bet:</div></div>")
    options = {f"{r['ticker']}|{r['side']}": r for r in rows[:200]}
    if not options:
        st.caption("No recent bets yet. They show up a minute or two after the app starts.")
        return
    def on_pick():
        ss = st.session_state
        if ss.q_pick in options:
            _set_ctx(_ctx_from(options[ss.q_pick], scored_at))
        ss.q_pick = None

    st.selectbox("Recent bet", [None] + list(options), index=0, key="q_pick", on_change=on_pick,
                 format_func=lambda k: "Recent bets…" if k is None else _bet_label(options[k]),
                 label_visibility="collapsed")


def _header(ctx: dict) -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.html(f'<div class="qv tick">{html.escape(ctx["ticker"])}</div>', width="stretch")
        if st.button("✕ Clear", type="tertiary"):
            _set_ctx(None)
            st.rerun()


def _guess_price(client, ctx: dict) -> None:
    """No price came with the paste: start from the combo's newest fill."""
    try:
        trades = recent_trades(client, ctx["ticker"], limit=20)
    except Exception:
        trades = []
    for t in trades:
        side = t.get("taker_outcome_side") or t.get("taker_side")
        if ctx["side"] in (None, side):
            ctx["side"] = side
            ctx["price"] = float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])
            ctx["since"] = _parse_time(t.get("created_time"))  # so this fill doesn't count as proof it's live
            return
    ctx["side"] = ctx["side"] or "yes"


def _verdict_html(c: LiveCheck, then: dict | None, seen_at: datetime | None) -> str:
    now = c.checked_at
    s = c.scored
    word = c.verdict.replace(" ", "")
    out = [f'<div class="card verdict {word}"><b class="word">{html.escape(c.verdict)}</b>{html.escape(c.reason)}<br>']
    if c.best_now is not None:
        out.append(f'<span class="dim">Best now</span> <b>{mult(c.best_now)}</b> ({odds(c.best_now)}) · '
                   f'EV mid {_pct(c.ev_now.get("mid"))} · cross {_pct(c.ev_now.get("cross"))}<br>')
    out.append(f'<span class="dim">At {mult(c.price)}:</span> EV mid {_pct(s["ev_pct_mid"])} · '
               f'cross {_pct(s["ev_pct_crx"])} <span class="dim">· fair {mult(s["fair_mid"])} / {mult(s["fair_crx"])} · '
               f'checked {_clock(now, "%H:%M:%S")}</span></div>')

    if then is not None:
        out.append(_moves_html(c, then, seen_at))

    out.append(f"<h4>Combo book · buy {c.side.upper()}</h4>")
    if c.book_levels:
        out.append('<div class="chips">' + "".join(
            f'<span class="{"pos" if p <= c.price + 1e-9 else ""}">{mult(p)} × {n:,.0f}</span>'
            for p, n in c.book_levels) + "</div>")
    else:
        out.append('<div class="dim">No resting orders. Makers only answer RFQs on this one.</div>')

    fills = c.fills[:8]
    out.append(f"<h4>Newest fills · {len(c.side_fills)} on {c.side.upper()} in the last {len(c.fills)} trades</h4>")
    if fills:
        rows = "".join(
            f'<tr><td>{_ago(f.ts, now)} ago</td><td class="{"cy" if f.side == c.side else "dim"}">{f.side.upper()}</td>'
            f'<td class="{"pos" if f.side == c.side and f.price <= c.price * 1.02 else ""}">{mult(f.price)}</td>'
            f"<td>{f.contracts:,.0f}</td><td>${f.stake:,.2f}</td></tr>" for f in fills)
        out.append(f"<table><tr><th>When</th><th>Side</th><th>Odds</th><th>Contracts</th><th>$</th></tr>{rows}</table>")
    else:
        out.append('<div class="dim">No fills on this combo.</div>')

    legs = s["leg_detail"]
    out.append(f"<h4>Legs · {len(legs)}</h4><table><tr><th>Leg</th><th>Bid</th><th>Ask</th><th>Top</th></tr>")
    for l in legs:
        live = '<span class="ld"></span>' if leg_live(l) else ""
        thin = (l["spread"] or 1) > 0.03 + 1e-9 or min(l["top_bid_size"], l["top_ask_size"]) < 50
        out.append(
            f'<tr><td>{live}<span class="cy">{l["side"].upper()}</span> {html.escape(l["leg"])}'
            f'<small>{html.escape(l["event_title"])}</small></td><td>{_c(l["bid"])}</td><td>{_c(l["ask"])}</td>'
            f'<td class="{"warn" if thin else ""}">{min(l["top_bid_size"], l["top_ask_size"]):,.0f}</td></tr>')
    out.append("</table>")
    return '<div class="qv">' + "".join(out) + "</div>"


def _moves_html(c: LiveCheck, then: dict, seen_at: datetime | None) -> str:
    """How the line moved since CapyEV scored the bet: fair odds and EV then vs now, and the legs that moved."""
    s = c.scored
    when = _clock(seen_at) if seen_at else "then"
    ago = f" ({_ago(seen_at, c.checked_at)} ago)" if seen_at else ""
    ev_then = then.get("ev_pct_mid")
    # EV now at the bet's own price (the price box may hold another one), so only the legs move it.
    ev_now = s["fair_mid"] / then["price"] - 1 if s["fair_mid"] and then.get("price") else None
    out = [f'<div class="card"><span class="dim">Since {when}{ago}:</span> fair {mult(then.get("fair_mid"))} → '
           f'<b>{mult(s["fair_mid"])}</b> · EV at {mult(then["price"])} {_pct(ev_then)} → <b>{_pct(ev_now)}</b>']
    moved = [m for m in leg_moves(then, s) if m.moved]
    if moved:  # green when the move helps this bet: legs up for YES, down for NO
        out.append("<br>" + " · ".join(
            f'<span class="{"pos" if (m.delta > 0) == (c.side == "yes") else "neg"}">{"▲" if m.delta > 0 else "▼"}'
            f'</span> {html.escape(m.name)} {m.then * 100:.0f}% → {m.now * 100:.0f}%' for m in moved))
    elif ev_then is not None and ev_now is not None:
        out.append('<br><span class="dim">No leg moved more than a point and a half.</span>')
    if ev_then is not None and ev_now is not None and ev_then > 0 >= ev_now:
        out.append('<br><span class="warn">The edge is gone at this price.</span>')
    out.append("</div>")
    return "".join(out)


# ---------- quotes ----------
def _quotes_section(desk, c: LiveCheck, side: str, can_bet: bool) -> None:
    ss = st.session_state
    unit, kelly = ss.q_unit, ss.q_kelly
    fair = c.scored["fair_mid"]
    ref = c.best_now or c.price
    k_dollars = kelly_stake(fair, ref, unit=unit, kelly=kelly)
    k_units = k_dollars / unit if unit else 0
    kp = min(round(k_units, 2), MAX_BET_UNITS) if k_units >= 0.02 else None  # the Kelly pill, at most the cap
    key = f"q_size_{c.ticker}_{side}"
    if key not in ss:
        ss[key] = kp or 0.25
    pills = sorted(set(UNIT_PILLS + ([kp] if kp else []) + ([ss[key]] if ss[key] else [])))
    kname = "Kelly (capped)" if k_units > MAX_BET_UNITS else "Kelly"
    size = st.pills("Size", pills, key=key, label_visibility="collapsed",
                    format_func=lambda u: (f"{kname} {u:g}u" if kp and abs(u - kp) < 0.005 else f"{u:g}u")
                    + f" ${u * unit:,.0f}") or ss[key] or 0.25
    get = st.button(f"Get quotes for ${size * unit:,.0f}", type="primary", width="stretch")
    # \$: a caption with two dollar signs would render the text between them as math.
    st.caption(f"Kelly ({kelly:g}×, 1u = \\${unit:,.0f}) at {mult(ref)}: "
               + (f"{k_units:.2f}u (\\${k_dollars:,.0f})" if k_dollars > 0 else "no bet, the edge isn't there")
               + (f", capped at {MAX_BET_UNITS:g}u here" if k_units > MAX_BET_UNITS else "")
               + " · sizes include Kalshi's fee")

    rfqs = ss.setdefault("q_rfqs", {})
    if get or ss.get("q_auto") != c.ticker:
        ss.q_auto = c.ticker  # one automatic request per pasted combo
        _start_rfq(desk, c.ticker, side, size * unit, "This combo", c.scored["fair_mid"], c.scored["fair_crx"],
                   owner=c.ticker)

    @st.fragment(run_every=POLL_SECONDS if any(not r.get("closed") for r in rfqs.values()) else None)
    def quotes_part():
        _poll(desk)
        live = [r for r in ss.get("q_rfqs", {}).values() if r["side"] == side]
        if not live:
            return
        quotes = []
        for r in live:
            for raw in r["quotes"]:
                q = quote_for(raw, side)
                if q and q.status in ("open", "") and q.contracts > 0:
                    quotes.append((r, q))
        quotes.sort(key=lambda x: ((x[0]["fair"] or 0) / x[1].all_in, -x[1].all_in), reverse=True)
        st.html(_quotes_html(quotes, live, unit, kelly))
        open_quotes = [(r, q) for r, q in quotes if not r["closed"]]
        bet = ss.get("q_bet")
        bet = bet if bet and bet.get("owner") == c.ticker else None  # a bet on this combo, not the last one
        if open_quotes and can_bet:
            best = {}  # the best quote on each combo (this one, and with extra legs)
            for r, q in open_quotes:
                best.setdefault(r["ticker"], (r, q))
            _bet_buttons(desk, list(best.values()), unit)
        elif quotes and not can_bet:
            st.caption("To bet from here, add TRADE_PIN to the app's secrets.")
        elif quotes and not bet and all(r["closed"] for r in live):
            st.caption("These quotes have closed. Get quotes again to bet.")
        if bet:
            (st.success if bet["ok"] else st.error)(bet["msg"])

    quotes_part()


def _start_rfq(desk, ticker: str, side: str, dollars: float, label: str, fair, fair_crx, owner: str) -> None:
    """Ask makers for quotes on `ticker`. `owner` is the pasted combo it's for (itself, or the one it adds legs to)."""
    ss = st.session_state
    if isinstance(desk, DemoRfqDesk):  # its makers price off the YES fair
        desk.fair[ticker] = (fair if side == "yes" else 1 - fair) if fair else 0.3
    rfqs = ss.setdefault("q_rfqs", {})
    old = rfqs.pop(ticker, None)
    if old and not old.get("closed"):
        try:
            desk.cancel(old["id"])
        except Exception:
            pass
    ss.pop("q_confirm", None)
    try:
        rid = desk.create(ticker, dollars)
    except RfqError as exc:
        st.error(f"Kalshi refused the request for quotes. {explain(exc)}")
        if exc.code == "INCORRECT_API_KEY_SIGNATURE" or exc.status == 401:
            ss.q_keycheck_open = True
        return
    REAPER.add(desk, rid)
    rfqs[ticker] = {"id": rid, "at": time.time(), "ticker": ticker, "side": side, "dollars": dollars, "owner": owner,
                    "label": label, "fair": fair, "fair_crx": fair_crx, "quotes": [], "closed": False}


def _close_others(desk, ticker: str | None) -> None:
    """Cancel open RFQs left from a combo you've moved on from."""
    rfqs = st.session_state.get("q_rfqs", {})
    for key in [k for k, r in rfqs.items() if r.get("owner") != ticker]:
        r = rfqs.pop(key)
        if not r.get("closed") and desk is not None:
            try:
                desk.cancel(r["id"])
            except Exception:
                pass


def _poll(desk) -> None:
    for r in st.session_state.get("q_rfqs", {}).values():
        if r.get("closed"):
            continue
        if time.time() - r["at"] >= RFQ_SECONDS:
            try:
                desk.cancel(r["id"])
            except Exception:  # already closed or filled
                pass
            r["closed"] = True
            continue
        try:
            r["quotes"] = desk.quotes(r["id"])
        except RfqError as exc:
            r["error"] = explain(exc)


def _quotes_html(quotes: list, rfqs: list[dict], unit: float, kelly: float) -> str:
    status = " · ".join(
        f'{html.escape(r["label"])} ${r["dollars"]:,.0f}: '
        + (f'open {time.time() - r["at"]:.0f}s' if not r["closed"] else "closed")
        + (f' <span class="warn">{html.escape(r["error"])}</span>' if r.get("error") else "") for r in rfqs)
    head = f'<h4>Quotes received ({len(quotes)}) · {status}</h4>'
    if not quotes:
        waiting = any(not r["closed"] for r in rfqs)
        return (f'<div class="qv">{head}<div class="dim">'
                + ("Waiting for makers… they usually answer within a few seconds." if waiting else
                   "No maker quoted. Makers skip some combos (a leg in play, an odd size): try another size or "
                   "Get quotes again.") + "</div></div>")
    many = len({r["ticker"] for r in rfqs}) > 1
    rows = []
    for i, (r, q) in enumerate(quotes):
        ev_m = r["fair"] / q.all_in - 1 if r["fair"] else None
        ev_c = r["fair_crx"] / q.all_in - 1 if r["fair_crx"] else None
        k = kelly_stake(r["fair"], q.all_in, unit=unit, kelly=kelly)
        what = f'<small>{html.escape(r["label"])}</small>' if many else f"<small>{html.escape(q.maker)}</small>"
        rows.append(
            f'<tr class="{"best" if i == 0 else ""}"><td><b>{mult(q.all_in)}</b> {odds(q.all_in)}{what}</td>'
            f"<td>${q.cost:,.0f}<small>wins ${q.contracts - q.cost:,.0f}</small></td>"
            f"<td>{_pct(ev_m)}<small>{_pct(ev_c)}</small></td>"
            f'<td>{_units(k, unit) if k > 0 else "–"}<small>{_units(q.cost, unit)} asked</small></td></tr>')
    return (f'<div class="qv">{head}<table><tr><th>Odds (fee in)</th><th>Cost</th><th>EV mid<br>cross</th>'
            f'<th>Kelly</th></tr>{"".join(rows)}</table></div>')


def _bet_buttons(desk, quotes: list, unit: float) -> None:
    ss = st.session_state
    pending = ss.get("q_confirm")
    if pending and pending["rfq"].get("closed"):  # its request expired while the card was up
        ss.pop("q_confirm", None)
        pending = None
    if pending:
        r, q = pending["rfq"], pending["quote"]
        ev = r["fair"] / q.all_in - 1 if r["fair"] else None
        st.html(f'<div class="qv"><div class="card {"LIVE" if ev and ev > 0 else "MOVED"}"><span class="big">Buy {q.side.upper()} '
                f'{q.contracts:,.2f} @ {mult(q.all_in)} ({odds(q.all_in)})</span><br>Cost ${q.cost:,.2f} '
                f'({_units(q.cost, unit)}) · pays ${q.contracts:,.2f} if it hits · EV mid {_pct(ev)}'
                + ('<br><span class="warn">This quote is −EV at the legs\' mid.</span>' if ev is not None and ev <= 0
                   else "")
                + ('<br><span class="dim">This maker also bids your side. CapyEV reads Kalshi\'s accept as "buy '
                   f'{q.side.upper()}" (from its docs; no bet has checked it yet), so check the fill below.</span>'
                   if q.two_sided else "") + f'<br><span class="dim">{html.escape(r["label"])} · {html.escape(r["ticker"])}</span></div></div>')
        # Callbacks, not st.rerun: they run before the next run, whether Streamlit reruns the fragment or the page.
        with st.container(horizontal=True):
            st.button("Confirm bet", type="primary", key="q_yes", on_click=_confirm, args=(desk, r, q))
            st.button("Cancel", key="q_no", on_click=ss.pop, args=("q_confirm", None))
        return
    with st.container(horizontal=True):
        for i, (r, q) in enumerate(quotes):
            label = f"Bet ${q.cost:,.0f} @ {mult(q.all_in)}" + (f" ({r['label']})" if len(quotes) > 1 else "")
            st.button(label, type="primary" if i == 0 else "secondary", key=f"q_bet_{q.id}", on_click=_pick,
                      args=(r, q, unit))


def _pick(r: dict, q, unit: float) -> None:
    ss = st.session_state
    if q.cost > MAX_BET_UNITS * unit * 1.01:  # 1%: a quote at the cap can come back a few cents over
        ss.q_bet = {"ok": False, "owner": r["owner"], "msg": f"That's over the {MAX_BET_UNITS:g}u cap for one bet "
                                                             "here. Pick a smaller size and get quotes again."}
    else:
        ss.q_confirm = {"rfq": r, "quote": q, "at": time.time()}
        ss.pop("q_bet", None)


def _confirm(desk, r: dict, q) -> None:
    ss = st.session_state
    if ss.pop("q_confirm", None) is None:  # one accept per card: a second tap does nothing
        return
    # Streamlit can end a callback at any session_state read or write when another tap comes in, so the
    # result is stored before the accept and filled in afterwards, with no session_state call after it.
    bet = {"ok": False, "owner": r["owner"],
           "msg": "The bet may have gone through: check the Kalshi app before betting again."}
    ss.q_bet = bet
    if r.get("closed") or time.time() - r["at"] >= RFQ_SECONDS:
        out = {"ok": False, "msg": "That request had closed, so nothing was bought. Get quotes again."}
    else:
        out = _take(desk, r, q)
    if r["label"] != "This combo":
        out["msg"] = f"{r['label']}: {out['msg']}"
    bet.update(out)


def _take(desk, r: dict, q) -> dict:
    """Accept a quote, wait for the maker to confirm and the trade to execute, then read the fill.

    When Kalshi doesn't answer the accept, the bet may still have gone through, so that's never
    reported as "nothing bought".
    """
    started = datetime.now(timezone.utc)
    r["closed"] = True  # the RFQ is done either way, unless Kalshi turns the accept down
    unsure = False
    try:
        desk.accept(r["id"], q.id, q.side)
    except RfqError as exc:
        if exc.code != "NETWORK" and exc.status is not None and 400 <= exc.status < 500:  # Kalshi said no
            r["closed"] = False
            return {"ok": False, "msg": f"Kalshi didn't take the bet. {explain(exc)}"}
        unsure = True  # no answer, a 5xx, or a 2xx we couldn't read
    except Exception:
        unsure = True
    status, t0 = ("unknown" if unsure else "accepted"), time.time()
    while time.time() - t0 < ACCEPT_WAIT:
        try:
            status = desk.quote(r["id"], q.id).get("status") or status
        except Exception:  # keep trying: the bet may have gone through
            pass
        if status in ("executed", "cancelled"):
            break
        time.sleep(0.5)
    if status == "cancelled":
        return {"ok": False, "msg": "The maker didn't confirm in time, so nothing was bought. Get quotes again."}
    fills = []
    try:
        fills = [f for f in desk.fills(r["ticker"])
                 if (_parse_time(f.get("created_time")) or started) >= started - timedelta(seconds=5)]
    except Exception:
        pass
    if fills:
        f = fills[0]
        fside = f.get("outcome_side") or f.get("side")
        n = float(f.get("count_fp") or f.get("count") or 0)
        price = float(f.get("yes_price_dollars" if fside == "yes" else "no_price_dollars") or 0)
        warn = "" if fside == q.side else f" ⚠ Kalshi filled {str(fside).upper()}, not {q.side.upper()}: check the app."
        return {"ok": fside == q.side, "msg": f"Filled: {str(fside).upper()} {n:,.2f} @ {mult(price)} before the fee "
                                              f"(${n * price:,.2f} + fee).{warn}"}
    if status == "executed":
        return {"ok": True, "msg": "Executed. The fill should show in the Kalshi app in a moment."}
    if unsure:
        return {"ok": False, "msg": "Lost contact with Kalshi during the bet, so it may have gone through. Check the "
                                    "Kalshi app before betting again."}
    return {"ok": False, "msg": f"Accepted, but the trade hadn't executed after {ACCEPT_WAIT}s (status: {status}). "
                                "Check the Kalshi app before betting again."}


# ---------- Kalshi app ----------
def _open_in_kalshi(c: LiveCheck) -> None:
    legs = c.scored["leg_detail"]
    with st.container(horizontal=True, vertical_alignment="center"):
        st.link_button("Bet on kalshi.com", bet_url(c.ticker), icon=":material/open_in_new:")
        st.caption("Opens Kalshi's combo builder in your browser with this combo in the slip. The Kalshi app "
                   "can't open a combo from a link." + (" :orange[It may start on YES: switch the slip to NO.]"
                                                        if c.side == "no" else ""))
    with st.expander("Legs to copy"):
        st.code("\n".join(f"{l['side'].upper()} {l['leg']} ({l['event_title']})" for l in legs), language=None)


# ---------- extra legs ----------
def _extra_legs_section(desk, client, c: LiveCheck, pool, starts) -> None:
    ss = st.session_state
    with st.expander("Add legs: does a bigger combo get better odds?", expanded=bool(ss.get("q_extra"))):
        st.caption("Near-certain legs (80–98%) on other games, from legs people combined in the last hour. "
                   "Makers price combos their own way, so adding one sometimes costs less than its odds. "
                   "Quote both and compare EV.")
        markets, events = pool() if pool else ({}, {})
        combo_legs = [{"market_ticker": l["ticker"], "event_ticker": l["ticker"].rsplit("-", 1)[0],
                       "side": l["side"]} for l in c.scored["leg_detail"]]
        market = (ss.get("q_legs") or {}).get(c.ticker)
        if market:
            combo_legs = legs_of(market[1][0]) or combo_legs
        cands = extra_legs(markets, events, combo_legs, game_start=starts)
        if not cands:
            st.caption("No suggestions yet: the pool fills as combos come in.")
            return
        sports = ["All"] + sorted({x.sport for x in cands}, key=lambda s: (s == "Other", s))
        sport = st.pills("Sport", sports, default="All", key="q_sport", label_visibility="collapsed") or "All"
        shown = [x for x in cands if sport in ("All", x.sport)][:24]
        by_key = {f"{x.market_ticker}|{x.side}": x for x in cands}
        picked = st.pills("Legs", list(dict.fromkeys([k for k in ss.get("q_extra", []) if k in by_key]
                                                     + [f"{x.market_ticker}|{x.side}" for x in shown])),
                          selection_mode="multi", key="q_extra", label_visibility="collapsed",
                          format_func=lambda k: _leg_chip(by_key[k]))
        extra = [by_key[k] for k in picked or [] if k in by_key]
        if not extra:
            return
        try:  # the pool can be a few minutes old: price the picked legs from their books now
            fresh = client.get_markets([x.market_ticker for x in extra])
            now = {f"{x.market_ticker}|{x.side}": x for x in extra_legs(fresh, events, combo_legs, game_start=starts,
                                                                         lo=0, hi=1, max_spread=1, min_size=0)}
        except Exception:
            now = None
            st.caption("Couldn't re-price the legs just now, so these use prices from the last few minutes.")
        if now is not None:
            gone = [x for x in extra if f"{x.market_ticker}|{x.side}" not in now]
            if gone:  # closed, no book, or flipped to the other side
                st.caption("Left out (closed, no book now, or the other side is the favorite now): "
                           + ", ".join(x.name.replace("$", "\\$") for x in gone))
            extra = [now[f"{x.market_ticker}|{x.side}"] for x in extra if f"{x.market_ticker}|{x.side}" in now]
            if not extra:
                return
        fair, crx = c.scored["fair_mid"], c.scored["fair_crx"]
        for x in extra:
            fair = fair * x.prob if fair else None
            crx = crx * x.bid if crx else None
        st.html(f'<div class="qv">With {len(extra)} more leg{"s" if len(extra) > 1 else ""}: fair '
                f'{mult(c.scored["fair_mid"])} → <b>{mult(fair)}</b> (mid) · {mult(crx)} (cross)</div>')
        if desk is None:
            return
        collection = market[1][0].get("mve_collection_ticker") if market else None
        if st.button("Quote with these legs", type="primary", disabled=not collection):
            try:
                ticker = desk.create_combo(collection, combo_legs + [x.as_leg() for x in extra])
            except RfqError as exc:
                st.error(f"Kalshi wouldn't make that combo. {explain(exc)}")
                return
            rfqs = ss.get("q_rfqs", {})
            dollars = next((r["dollars"] for r in rfqs.values() if r["ticker"] == c.ticker), 0.25 * ss.q_unit)
            _start_rfq(desk, ticker, "yes", dollars, f"+{len(extra)} leg{'s' if len(extra) > 1 else ''}", fair, crx,
                       owner=c.ticker)
            st.rerun()


def _leg_chip(x) -> str:
    live = "🔴 " if x.live else ""
    return f"{live}{x.side.upper()} {x.name} · {x.prob * 100:.0f}%"


# ---------- settings ----------
def _settings() -> None:
    ss = st.session_state

    def keep():  # widget values vanish on runs that don't draw them, so they're copied to q_unit / q_kelly
        ss.q_unit, ss.q_kelly = ss.q_unit_w, ss.q_kelly_w

    with st.expander(f"Sizing: 1u = ${ss.q_unit:,.0f} · {ss.q_kelly:g} Kelly"):
        with st.container(horizontal=True):
            st.number_input("1 unit ($)", min_value=10.0, step=100.0, value=float(ss.q_unit), key="q_unit_w",
                            on_change=keep)
            st.select_slider("Kelly", [0.1, 0.2, 0.25, 0.33, 0.5, 1.0], value=ss.q_kelly, key="q_kelly_w",
                             on_change=keep)
        st.caption(f"Kelly sizing assumes 1u is 1% of bankroll, like the Combos page. One bet here is capped at "
                   f"{MAX_BET_UNITS:g}u.")


# ---------- API key ----------
def _key_section(desk) -> None:
    if desk is None:
        return
    ss = st.session_state
    with st.expander("API key check", expanded=bool(ss.get("q_keycheck_open"))):
        if st.button("Check my key") or (ss.get("q_keycheck_open") and not ss.get("q_keycheck")):
            ss.q_keycheck = desk.key_check()
        k = ss.get("q_keycheck")
        if not k:
            st.caption("Signs two harmless requests with the app's key to see whether Kalshi accepts it for reading "
                       "and for trading.")
            return
        ok = lambda v: "✅" if v == "ok" else f"❌ {v}"  # noqa: E731
        st.markdown(f"- Key: **{k['kind']}**, key ID ending **{k['key_id_end']}**, public key fingerprint "
                    f"**{k['fingerprint']}**\n- Read: {ok(k['read'])} · Trade: {ok(k['write'])}"
                    + (f" · scopes {', '.join(k['scopes'])}" if k.get("scopes") else ""))
        if "INCORRECT_API_KEY_SIGNATURE" in (k["read"], k["write"]):
            st.warning("Kalshi doesn't recognize this signature: the key ID and the private key in the app's secrets "
                       "don't belong together. Paste both again from the same Kalshi key (Manage app → Settings → "
                       "Secrets), then reboot the app.")


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
