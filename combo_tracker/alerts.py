"""Phone alerts for big +EV combo bets, sent through ntfy (https://ntfy.sh).

ntfy is free and needs no account: install its app, subscribe to a topic name only
you know, and every message posted to that topic shows up as a push notification.
scripts/alert_bot.py runs the loop; this module picks the bets and writes the messages.
"""

from __future__ import annotations

import json
import re

import requests

from .format import bet_card, fmt_odds
from .links import bet_url
from .view import is_live

NTFY_URL = "https://ntfy.sh"
THIN_SPREAD = 0.03  # same default as the Combos page's Hide thin
THIN_SIZE = 50


def key(row: dict) -> str:
    """One alert per combo and side: more fills on the same bet don't ping again."""
    return f"{row['ticker']}|{row['side']}"


def ev_of(row: dict, basis: str = "mid") -> float | None:
    return row["ev_pct_mid"] if basis == "mid" else row["ev_pct_crx"]


def pick(rows: list[dict], min_stake: float, min_ev: float, sent: set[str], basis: str = "mid") -> list[dict]:
    """Bets of at least `min_stake` dollars with EV above `min_ev` (0.10 = 10%) not alerted yet, best first."""
    hits = [r for r in rows if r["stake"] >= min_stake and (ev_of(r, basis) or -1) > min_ev and key(r) not in sent]
    return sorted(hits, key=lambda r: ev_of(r, basis), reverse=True)


def thin(row: dict) -> bool:
    return any(l["spread"] is None or l["spread"] > THIN_SPREAD + 1e-9 or min(l["top_bid_size"], l["top_ask_size"]) < THIN_SIZE
               for l in row["leg_detail"])


def tier(ev: float) -> tuple[int, str]:
    """ntfy priority and emoji tag: the bigger the edge, the louder the ping."""
    if ev >= 5:
        return 5, "rocket"
    if ev >= 1:
        return 5, "moneybag"
    if ev >= 0.5:
        return 4, "fire"
    return 3, "green_circle"


def title(row: dict, basis: str = "mid") -> str:
    """'+812% EV · $1,250 YES 4-leg combo', the line a lock screen shows."""
    return (f"{ev_of(row, basis) * 100:+.0f}% EV · ${row['stake']:,.0f} {row['side'].upper()} "
            f"{row['legs']}-leg combo")


def body(row: dict) -> str:
    price = row["price"]
    mult = f"{1 / price:.1f}x" if price else "?"
    notes = [f"{row['minutes_ago']:.0f} min ago", "LIVE" if is_live(row) else "pregame"]
    if row["same_game_legs"]:
        notes.append("same game")
    if thin(row):
        notes.append("thin leg book")
    if (row["ev_pct_mid"] or 0) > 2:
        notes.append("check the book, +200% edges are often stale")
    return "\n".join([
        f"${row['stake']:,.0f} @ {mult} ({fmt_odds(price)}) → ${row['max_payout']:,.0f}",
        " · ".join(notes),
        "",
        bet_card(row),
    ])


def message(row: dict, topic: str, basis: str = "mid", app_url: str | None = None) -> dict:
    """The JSON ntfy takes on its root URL (title, body, tap action, buttons)."""
    priority, tag = tier(ev_of(row, basis))
    actions = [{"action": "view", "label": "Bet in Kalshi", "url": bet_url(row["ticker"])}]
    if app_url:
        q = f"t={row['ticker']}&side={row['side']}&p={row['price']:.4f}&n={row['contracts']:.0f}"
        actions.append({"action": "view", "label": "Quote it", "url": f"{app_url.rstrip('/')}/quote?{q}"})
    return {"topic": topic, "title": title(row, basis), "message": body(row), "priority": priority,
            "tags": [tag], "click": bet_url(row["ticker"]), "actions": actions}


_SIDE = re.compile(r"\b(YES|NO)\b")


def sent_keys(topic: str, since: str = "12h", session=requests) -> set[str]:
    """Alerts already on the topic (ntfy keeps 12 hours), so a restarted bot doesn't repeat them."""
    resp = session.get(f"{NTFY_URL}/{topic}/json", params={"poll": "1", "since": since}, timeout=15)
    resp.raise_for_status()
    out = set()
    for line in resp.text.splitlines():
        msg = json.loads(line)
        side = _SIDE.search(msg.get("title", ""))
        click = msg.get("click", "")
        if msg.get("event") == "message" and side and "/markets_by_ticker/" in click:
            out.add(f"{click.rsplit('/', 1)[1]}|{side.group(1).lower()}")
    return out


def send(msg: dict, session=requests) -> None:
    session.post(NTFY_URL, json=msg, timeout=15).raise_for_status()
