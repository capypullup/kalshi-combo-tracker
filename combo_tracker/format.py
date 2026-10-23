"""Plain-text bet card, e.g.

YES Combo +4579
────────────
NO Mookie Betts: 2+ (Milwaukee vs Los Angeles D: Home Runs)
────────────
EV MID +3610.1% (fair -383 / 1.26x) · 20u
EV CRX +3568.4% (fair -363 / 1.28x) · 19u
"""

from __future__ import annotations

import math

from .engine import american_odds

RULE = "────────────"


def fmt_odds(p: float | None) -> str:
    odds = american_odds(p)
    if odds is None:
        return "n/a"
    return f"+{odds}" if odds > 0 else str(odds)


def fmt_mult(p: float | None) -> str:
    """Decimal odds as a payout multiplier: 0.8 -> '1.25x', 0.05 -> '20.0x' (ui/table.js mult() matches)."""
    if p is None or p <= 0:
        return "n/a"
    m = 1 / p
    return f"{round(m)}x" if m >= 100 else f"{m:.1f}x" if m >= 10 else f"{m:.2f}x"


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def units(kelly: float | None, kelly_mult: float = 0.25, unit_pct: float = 1.0) -> float | None:
    """Kelly stake in units, where one unit is `unit_pct`% of bankroll."""
    if _missing(kelly):
        return None
    return kelly * kelly_mult * 100 / unit_pct


def fmt_units(u: float | None) -> str:
    """'20u' for big stakes, '0.88u' for small ones."""
    if u is None:
        return "–"
    return f"{u:.0f}u" if u >= 10 else f"{u:.2f}u"


def ev_line(name: str, ev_pct, fair, kelly, kelly_mult: float, unit_pct: float) -> str:
    if _missing(ev_pct) or _missing(fair):
        return f"EV {name} n/a (a leg has no quote)"
    return f"EV {name} {ev_pct * 100:+.1f}% (fair {fmt_odds(fair)} / {fmt_mult(fair)}) · {fmt_units(units(kelly, kelly_mult, unit_pct))}"


def bet_card(row: dict, kelly_mult: float = 0.25, unit_pct: float = 1.0) -> str:
    lines = [f"{row['side'].upper()} Combo {fmt_odds(row['price'])}", RULE]
    for leg in row["leg_detail"]:
        lines.append(f"{leg['side'].upper()} {leg['leg']} ({leg['event_title']})")
    lines += [
        RULE,
        ev_line("MID", row["ev_pct_mid"], row["fair_mid"], row["kelly_mid"], kelly_mult, unit_pct),
        ev_line("CRX", row["ev_pct_crx"], row["fair_crx"], row["kelly_crx"], kelly_mult, unit_pct),
    ]
    return "\n".join(lines)
