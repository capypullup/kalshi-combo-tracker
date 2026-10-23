"""The Home page's picks: the best +EV combos right now, in the compact JSON its cards render."""

from __future__ import annotations

from combo_tracker.sports import league_of
from combo_tracker.view import flags, is_live, leg_live, num

MAX_PICKS = 60  # cards sent to the phone; the page filters these in the browser


def _key(r: dict) -> tuple:
    """Best first: EV crossing the spread (the conservative number), then EV at mid."""
    evc, evm = r["ev_pct_crx"], r["ev_pct_mid"]
    return (evc if evc is not None else -9, evm if evm is not None else -9)


def picks(rows: list[dict], limit: int = MAX_PICKS) -> list[dict]:
    """Bets whose fair price at mid beats what was paid, every leg quoted, best EV first."""
    good = [r for r in rows if r["ev_pct_mid"] is not None and r["ev_pct_mid"] > 0
            and not r["missing_quotes"] and not r["inactive_legs"]]
    return sorted(good, key=_key, reverse=True)[:limit]


def card(r: dict) -> dict:
    """One Home card: the EV pair, the bet, its games' start, and its legs."""
    starts = [l["start"] for l in r["leg_detail"] if l.get("start")]
    return {
        "ticker": r["ticker"], "side": r["side"], "t": int(r["last_time"].timestamp() * 1000),
        "price": num(r["price"]), "n": num(r["contracts"], 2), "stake": num(r["stake"], 2),
        "evm": num(r["ev_pct_mid"]), "evc": num(r["ev_pct_crx"]), "fm": num(r["fair_mid"]), "fc": num(r["fair_crx"]),
        "km": num(r.get("kelly_mid")), "kc": num(r.get("kelly_crx")), "L": r["legs"], "G": r["games"], "sg": r["same_game_legs"], "live": is_live(r), "flags": flags(r),
        "start": int(min(starts).timestamp() * 1000) if starts else None,
        "legs": [{"s": l["side"], "n": l["leg"], "e": l["event_title"], "lv": leg_live(l),
                  **dict(zip(("lg", "spt"), league_of(l["ticker"])))}
                 for l in r["leg_detail"]],
    }


def pack_home(rows: list[dict]) -> list[dict]:
    return [card(r) for r in picks(rows)]
