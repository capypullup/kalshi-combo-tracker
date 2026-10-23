"""The Tools page. Its first tool is a hedge calculator: type your bet and the other side's
odds, and it shows what to bet to lock in a profit (or get your stake back) and what you end
up with either way."""

from __future__ import annotations

import streamlit as st

from .hedge import MODES, hedge, parse_odds
from .quote import odds

CSS = """<style>
.hg {font-family: "Nunito", ui-rounded, "SF Pro Rounded", ui-sans-serif, -apple-system, system-ui, sans-serif; color: #e6fbff;
     font-variant-numeric: tabular-nums; font-size: 13px; line-height: 1.3;}
.hg .dim {color: #6f8f99;} .hg .pos {color: #2ee6a6;} .hg .neg {color: #ff5c6c;} .hg .cy {color: #00e5ff;}
.hg .warn {color: #ffc857;}
.hg .read {font-size: 12px; color: #6f8f99; margin: -4px 0 4px 2px;}
.hg .card {border: 1px solid #00e5ff; border-radius: 16px; padding: 10px 12px; margin: 4px 0 6px;
     background: rgba(0, 229, 255, 0.06);}
.hg .card .lbl {font-size: 12px; font-weight: 800; color: #6f8f99;}
.hg .card .big {font-size: 26px; font-weight: 900; color: #00e5ff; letter-spacing: -0.01em;}
.hg .card .sub {font-size: 12.5px; color: #8fb0b9;}
.hg .outs {display: flex; gap: 6px; margin: 6px 0;}
.hg .out {flex: 1; border: 1px solid #1b3540; border-radius: 14px; padding: 8px 10px;}
.hg .out .lbl {font-size: 12px; font-weight: 800; color: #6f8f99;}
.hg .out b {display: block; font-size: 20px; font-weight: 900;}
.hg .out small {color: #6f8f99; font-size: 11.5px;}
.hg .lock {font-size: 13px; margin: 4px 2px;}
</style>"""


def _money(x: float, sign: bool = True) -> str:
    s = f"${abs(x):,.2f}"
    return s if not sign else ("+" if x >= 0.005 else "−" if x <= -0.005 else "") + s


def _pl(x: float) -> str:
    return f'<span class="{"pos" if x >= 0.005 else "neg" if x <= -0.005 else "dim"}">{_money(x)}</span>'


def _read(price: float | None, text: str) -> str:
    """How we read the typed odds, so a 22 meant as +2200 doesn't slip by."""
    if price is None:
        return '<div class="hg"><div class="read warn">Type odds like +400, -150, 22¢ or 5x</div></div>' if text else ""
    return (f'<div class="hg"><div class="read">= {price * 100:.1f}¢ · {odds(price)} · '
            f'{1 / price:.2f}x</div></div>')


def hedge_tool() -> None:
    st.markdown("**Your bet**")
    with st.container(horizontal=True):
        stake = st.number_input("Stake ($)", min_value=0.0, value=100.0, step=10.0, key="hg_stake")
        text = st.text_input("Odds you got", value="+400", key="hg_odds",
                             help="American (+400, -150), Kalshi cents (20¢) or a multiplier (5x)")
    price = parse_odds(text)
    st.html(_read(price, text))

    st.markdown("**Hedge on the other side**")
    htext = st.text_input("Other side's odds now", value="60¢", key="hg_hodds",
                          help="On Kalshi this is the NO ask, or 100¢ minus the YES bid if you'd rather sell.")
    hprice = parse_odds(htext)
    st.html(_read(hprice, htext))

    mode = st.segmented_control("Hedge to", MODES, default=MODES[0], key="hg_mode", width="stretch") or MODES[0]
    amount = None
    if mode == "Custom":
        amount = st.number_input("Hedge amount ($)", min_value=0.0, value=round(stake / 2, 2) if stake else 0.0,
                                 step=10.0, key="hg_amount")
    fee = st.toggle("Hedge on Kalshi (include taker fee)", value=True, key="hg_fee")

    if not stake or price is None or hprice is None:
        return
    h = hedge(stake, price, hprice, mode, amount, kalshi_fee=fee)

    out = [CSS, '<div class="hg"><div class="card"><div class="lbl">Bet on the other side</div>'
           f'<div class="big">{_money(h.hedge, sign=False)}</div><div class="sub">at {odds(hprice)} '
           f'({hprice * 100:.1f}¢)']
    if fee:
        out.append(f" · ≈{h.contracts:,.0f} contracts · fee {_money(h.fee, sign=False)}")
    out.append(f' · pays {_money(h.hedge_payout, sign=False)}</div></div><div class="outs">'
               f'<div class="out"><div class="lbl">If your bet wins</div><b>{_pl(h.if_original_wins)}</b>'
               f'<small>was {_money(h.payout - h.stake)} unhedged</small></div>'
               f'<div class="out"><div class="lbl">If the hedge wins</div><b>{_pl(h.if_hedge_wins)}</b>'
               f'<small>was {_money(-h.stake)} unhedged</small></div></div>')
    if mode == "Lock profit":
        roi = h.worst / (h.stake + h.hedge + h.fee)
        if h.worst >= 0.005:
            out.append(f'<div class="lock">Locked in <b class="pos">{_money(h.worst)}</b> whichever way it goes '
                       f'<span class="dim">({roi * 100:.1f}% on {_money(h.stake + h.hedge + h.fee, sign=False)} '
                       f'staked)</span></div>')
        else:
            out.append(f'<div class="lock warn">Locking in now costs {_money(-h.worst, sign=False)}: the two prices '
                       f'add up to {(price + hprice) * 100:.1f}¢, over a dollar{" with the fee" if fee else ""}. '
                       f'Try Cover stake, or wait for a better price.</div>')
    st.html("".join(out) + "</div>")


TOOLS = {"Hedge calculator": hedge_tool}


def render() -> None:
    st.html('<div class="hg" style="font-size:20px;font-weight:900;margin:4px 2px 0">Tools</div>')
    pick = st.segmented_control("Tool", list(TOOLS), default=list(TOOLS)[0], key="tool",
                                label_visibility="collapsed") or list(TOOLS)[0]
    TOOLS[pick]()
    with st.expander("How the hedge works"):
        st.markdown(
            "- Prices are what $1 of payout costs. A bet of $100 at +400 (20¢) pays $500 if it wins.\n"
            "- **Lock profit** bets enough on the other side that both outcomes pay the same, so you keep the "
            "same profit whatever happens. On Kalshi that's as many NO contracts as you hold YES (or selling your "
            "YES at the bid, which comes to the same thing).\n"
            "- **Cover stake** bets just enough that if your bet loses, the hedge pays back what you staked. You "
            "can't lose money and keep most of the upside.\n"
            "- **Custom** shows the result for any hedge amount you type.\n"
            "- Locking in only makes money once the two prices add up to less than a dollar (about 100¢ minus "
            "the fee). With the Kalshi fee on, the hedge pays the taker fee: 7% × contracts × price × (1 − price).\n"
            "- Odds can be American (+400, -150; any number 100 or more counts as American), Kalshi cents "
            "(20¢ or a plain 20), or a multiplier (5x, or 5.0). The line under each box shows how it was read."
        )
