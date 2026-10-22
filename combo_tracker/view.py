"""Turn scored rows into the compact JSON the table component renders."""

from __future__ import annotations

import math

LIVE_HOURS = 3  # a leg expiring within this many hours is treated as a game in progress


def num(x, digits: int = 6):
    """JSON-safe number: None for missing or non-finite values."""
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(x, digits)


def flags(r: dict) -> str:
    out = []
    if r["same_game_legs"]:
        out.append("SG")
    if r["min_depth_ratio"] is not None and r["min_depth_ratio"] < 1:
        out.append("THIN")
    if r["missing_quotes"] or r["inactive_legs"]:
        out.append("NO QUOTE")
    if r["outside_band"] is False:
        out.append("IN BAND")
    if r["ev_pct_mid"] is not None and r["ev_pct_mid"] > 2:
        out.append("CHECK")  # +200% edges are usually a stale book or a misread side
    return " · ".join(out)


def is_live(r: dict) -> bool:
    """Best guess at whether a game in the combo has started (Kalshi gives no start time)."""
    first = r.get("hours_to_first_leg")
    return bool(r["inactive_legs"]) or (first is not None and first < LIVE_HOURS)


def slim_legs(r: dict) -> list[dict]:
    return [{"s": l["side"], "n": l["leg"], "e": l["event_title"], "m": num(l["mid"]), "b": num(l["bid"]),
             "a": num(l["ask"]), "d": num(l["depth_ratio"], 3)} for l in r["leg_detail"]]


def slim(r: dict) -> dict:
    """The fields the table needs, with short keys to keep the page light."""
    return {
        "id": f"{r['ticker']}|{r['side']}", "ticker": r["ticker"], "side": r["side"],
        "t": int(r["last_time"].timestamp() * 1000), "price": num(r["price"]), "n": num(r["contracts"], 2),
        "stake": num(r["stake"], 2), "evm": num(r["ev_pct_mid"]), "evc": num(r["ev_pct_crx"]),
        "km": num(r["kelly_mid"]), "kc": num(r["kelly_crx"]), "fm": num(r["fair_mid"]), "fc": num(r["fair_crx"]),
        "L": r["legs"], "G": r["games"], "sg": r["same_game_legs"], "live": is_live(r), "flags": flags(r),
        "net": num(r["ev_net"], 2), "fee": num(r["fee"], 2), "hrs": num(r["hours_to_resolve"], 2),
        "cb": num(r["combo_bid"]), "ca": num(r["combo_ask"]), "legs": slim_legs(r),
    }


def pack(rows: list[dict]) -> dict:
    """Slim rows with each leg and event name sent once; the table looks them up in `strs`.

    Leg and event titles repeat across hundreds of combos, so this roughly halves the
    data a phone downloads on every refresh.
    """
    index: dict[str, int] = {}

    def ref(s: str) -> int:
        return index.setdefault(s, len(index))

    out = []
    for r in rows:
        s = slim(r)
        del s["id"]  # ticker|side, rebuilt in the browser
        s["legs"] = [{**l, "n": ref(l["n"]), "e": ref(l["e"])} for l in s["legs"]]
        out.append(s)
    return {"rows": out, "strs": list(index)}
