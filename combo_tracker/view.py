"""Turn scored rows into the compact JSON the table component renders."""

from __future__ import annotations

import math

from .sports import league_of

LIVE_HOURS = 3  # without a scheduled start, a leg settling within this many hours counts as in progress


def num(x, digits: int = 6):
    """JSON-safe number: None for missing or non-finite values."""
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(x, digits)


def flags(r: dict) -> str:
    out = []
    if r["same_game_legs"]:
        out.append("SG")
    if r["missing_quotes"] or r["inactive_legs"]:
        out.append("NO QUOTE")
    if r["outside_band"] is False:
        out.append("IN BAND")
    if r["ev_pct_mid"] is not None and r["ev_pct_mid"] > 2:
        out.append("CHECK")  # +200% edges are usually a stale book or a misread side
    return " · ".join(out)


def leg_live(leg: dict) -> bool:
    """Has this leg's game started? Uses Kalshi's schedule when it lists the game."""
    if leg.get("status") not in (None, "active"):
        return True  # closed or settled
    if leg.get("hours_to_start") is not None:
        return leg["hours_to_start"] <= 0
    return leg.get("hours_to_expiry") is not None and leg["hours_to_expiry"] < LIVE_HOURS


def is_live(r: dict) -> bool:
    return any(leg_live(l) for l in r["leg_detail"])


def slim_legs(r: dict) -> list[dict]:
    """Per leg: side, name, event, mid/bid/ask, spread, smaller top-of-book size, live, start, league, sport."""
    out = []
    for l in r["leg_detail"]:
        league, sport = league_of(l["ticker"])
        out.append({"s": l["side"], "n": l["leg"], "e": l["event_title"], "m": num(l["mid"]), "b": num(l["bid"]),
                    "a": num(l["ask"]), "sp": num(l["spread"], 4), "z": num(min(l["top_bid_size"], l["top_ask_size"]), 0),
                    "lv": leg_live(l), "st": int(l["start"].timestamp() * 1000) if l.get("start") else None,
                    "lg": league, "spt": sport})
    return out


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
    """Slim rows with each leg, event, league and sport name sent once; the table looks them up in `strs`.

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
        s["legs"] = [{**l, **{k: ref(l[k]) for k in ("n", "e", "lg", "spt")}} for l in s["legs"]]
        out.append(s)
    return {"rows": out, "strs": list(index)}


def _scope(s: dict) -> dict:
    return {"n": s["n"], "mk": s["markets"], "vol": num(s["vol"], 2), "ev": num(s["ev"], 2), "cov": num(s["ev_cover"], 3),
            "evr": num(s["ev_roi"], 4), "pnl": num(s["pnl"], 2), "st": s["settled"], "pr": num(s["pnl_roi"], 4),
            "win": num(s["win_rate"], 4),
            "edge": num(s["edge"], 2), "z": num(s["z"], 2), "tk": num(s["taker_share"], 3), "open": num(s["open_vol"], 2),
            "first": int(s["first"] * 1000) if s["first"] else None, "last": int(s["last"] * 1000) if s["last"] else None}


def pack_bettors(rows: list[dict]) -> list[dict]:
    """Compact rows for the Bettors table."""
    out = []
    for r in rows:
        out.append({
            "nick": r["nick"], "src": r["src"], "hidden": r["hidden"], "missing": r["missing"],
            "pa": num(r["pnl_all"], 2), "p30": num(r["pnl_30d"], 2), "ma": r["markets_all"], "m30": r["markets_30d"],
            "fn": r["feed_n"], "fv": num(r["feed_vol"], 2), "fl": int(r["feed_last"] * 1000) if r["feed_last"] else None,
            **{k: _scope(r[k]) for k in ("all", "tennis", "all30", "tennis30")}, "done": r["history_done"],
            "code": r["code"], "linked": r["linked"], "codes": r["codes"], "sure": r["sure"],
            "recent": [{"t": int(f["t"] * 1000), "title": f["title"], "L": f["legs"], "s": f["side"], "p": num(f["price"], 4),
                        "n": num(f["n"], 2), "tk": f["taker"], "fair": num(f["fair"], 4), "ev": num(f["ev"], 2),
                        "pnl": num(f["pnl"], 2)} for f in r["recent"]],
        })
    return out
