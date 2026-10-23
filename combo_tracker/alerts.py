"""Phone alerts for big +EV combo bets, sent by a Telegram bot or through ntfy (https://ntfy.sh).

Both are free. Telegram has no daily cap; ntfy needs no account but its free tier
allows about 250 messages a day. scripts/alert_bot.py runs the loop; this module
picks the bets and writes the messages.
"""

from __future__ import annotations

import time

import requests

from .format import bet_card, fmt_odds
from .links import bet_url
from .view import is_live

NTFY_URL = "https://ntfy.sh"
TELEGRAM_URL = "https://api.telegram.org"
THIN_SPREAD = 0.03  # same default as the Combos page's Hide thin
THIN_SIZE = 50
# Leg series left out by default: crypto price markets (KXBTC also covers KXBTCD, KXBTC15M, ...).
SKIP_SERIES = ("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB", "KXHYPE", "KXADA", "KXLTC", "KXAVAX",
               "KXLINK", "KXSHIBA", "KXDOT", "KXSUI", "KXTRX", "KXXLM", "KXBCH", "KXTON", "KXPEPE")


def key(row: dict) -> str:
    """One alert per combo and side: more fills on the same bet don't ping again."""
    return f"{row['ticker']}|{row['side']}"


def ev_of(row: dict, basis: str = "mid") -> float | None:
    return row["ev_pct_mid"] if basis == "mid" else row["ev_pct_crx"]


def parse_tiers(text: str) -> list[tuple[float, float]]:
    """'500:20,1000:10' -> [(500, 0.20), (1000, 0.10)]: bets of $500+ need +20% EV, $1,000+ need +10%."""
    tiers = []
    for part in text.split(","):
        stake, ev = part.split(":")
        tiers.append((float(stake), float(ev) / 100))
    return sorted(tiers)


def min_ev_for(stake: float, tiers: list[tuple[float, float]]) -> float | None:
    """The EV bar for a bet this size (the biggest tier it reaches), or None when it's under every tier."""
    reached = [ev for floor, ev in tiers if stake >= floor]
    return reached[-1] if reached else None


def skipped(row: dict, series: tuple[str, ...] = SKIP_SERIES) -> bool:
    """Has a leg from a left-out series (crypto by default)?"""
    return any(l["ticker"].startswith(series) for l in row["leg_detail"])


def pick(rows: list[dict], tiers: list[tuple[float, float]], sent: set[str], basis: str = "mid",
         skip_thin: bool = True, skip_series: tuple[str, ...] = SKIP_SERIES) -> list[dict]:
    """Bets that clear their size tier's EV bar, not alerted yet, best first. Thin leg books and
    combos with a crypto leg are left out."""
    def wanted(r):
        bar = min_ev_for(r["stake"], tiers)
        return (bar is not None and (ev_of(r, basis) or -1) > bar and key(r) not in sent
                and not (skip_thin and thin(r)) and not skipped(r, skip_series))
    return sorted(filter(wanted, rows), key=lambda r: ev_of(r, basis), reverse=True)


def thin(row: dict) -> bool:
    return any(l["spread"] is None or l["spread"] > THIN_SPREAD + 1e-9 or min(l["top_bid_size"], l["top_ask_size"]) < THIN_SIZE
               for l in row["leg_detail"])


TIERS = [(5, 5, "rocket", "🚀"), (1, 5, "moneybag", "💰"), (0.5, 4, "fire", "🔥"), (-1, 3, "green_circle", "🟢")]


def tier(ev: float) -> tuple[int, str, str]:
    """ntfy priority, ntfy emoji tag and emoji: the bigger the edge, the louder the ping."""
    return next((p, tag, emoji) for floor, p, tag, emoji in TIERS if ev >= floor)


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


def buttons(row: dict, app_url: str | None = None) -> list[tuple[str, str]]:
    out = [("Bet in Kalshi", bet_url(row["ticker"]))]
    if app_url:
        q = f"t={row['ticker']}&side={row['side']}&p={row['price']:.4f}&n={row['contracts']:.0f}"
        out.append(("Quote it", f"{app_url.rstrip('/')}/quote?{q}"))
    return out


def message(row: dict, topic: str, basis: str = "mid", app_url: str | None = None) -> dict:
    """The JSON ntfy takes on its root URL (title, body, tap action, buttons)."""
    priority, tag, _ = tier(ev_of(row, basis))
    return {"topic": topic, "title": title(row, basis), "message": body(row), "priority": priority,
            "tags": [tag], "click": bet_url(row["ticker"]),
            "actions": [{"action": "view", "label": label, "url": url} for label, url in buttons(row, app_url)]}


def telegram_message(row: dict, chat_id: str, basis: str = "mid", app_url: str | None = None) -> dict:
    """sendMessage JSON: bold title line, the body, and link buttons under it."""
    emoji = tier(ev_of(row, basis))[2]
    head = f"{emoji} {title(row, basis)}"
    return {"chat_id": chat_id, "text": f"{head}\n{body(row)}", "entities": [{"type": "bold", "offset": 0, "length": len(head.encode("utf-16-le")) // 2}],
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": [[{"text": label, "url": url} for label, url in buttons(row, app_url)]]}}


class Ntfy:
    def __init__(self, topic: str, session=requests):
        self.topic, self.session = topic, session

    def send(self, row: dict, basis: str = "mid", app_url: str | None = None) -> None:
        self.session.post(NTFY_URL, json=message(row, self.topic, basis, app_url), timeout=15).raise_for_status()


class Telegram:
    """Sends as your bot. Without a chat ID it uses the chat that last messaged the bot."""

    def __init__(self, token: str, chat_id: str | None = None, session=requests):
        self.url, self.session = f"{TELEGRAM_URL}/bot{token}", session
        self.chat_id = chat_id or self.find_chat()

    def find_chat(self) -> str:
        resp = self.session.get(f"{self.url}/getUpdates", timeout=15)
        resp.raise_for_status()
        chats = [u["message"]["chat"]["id"] for u in resp.json().get("result", []) if "message" in u]
        if not chats:
            raise RuntimeError("no chat found: open your bot in Telegram, send it any message, then rerun "
                               "(or set TELEGRAM_CHAT_ID)")
        chat_id = str(chats[-1])
        self.post({"chat_id": chat_id, "text": f"CapyEV alerts are on. Your chat ID is {chat_id}: add it as the "
                                               "TELEGRAM_CHAT_ID secret so this keeps working after a day."})
        return chat_id

    def post(self, payload: dict) -> None:
        resp = self.session.post(f"{self.url}/sendMessage", json=payload, timeout=15)
        if resp.status_code == 429:  # Telegram says how long to wait
            time.sleep(float(resp.json().get("parameters", {}).get("retry_after", 3)))
            resp = self.session.post(f"{self.url}/sendMessage", json=payload, timeout=15)
        resp.raise_for_status()

    def send(self, row: dict, basis: str = "mid", app_url: str | None = None) -> None:
        self.post(telegram_message(row, self.chat_id, basis, app_url))
