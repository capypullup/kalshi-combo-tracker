"""Phone alerts for big +EV combo bets, sent by a Telegram bot or through ntfy (https://ntfy.sh).

Both are free. Telegram has no daily cap; ntfy needs no account but its free tier
allows about 250 messages a day. scripts/alert_bot.py runs the loop; this module
picks the bets and writes the messages.
"""

from __future__ import annotations

import time

import requests

from . import financial
from .format import bet_card, fmt_mult, fmt_odds, fmt_units, units
from .links import bet_url
from .sports import league_of
from .view import is_live

NTFY_URL = "https://ntfy.sh"
TELEGRAM_URL = "https://api.telegram.org"
THIN_SPREAD = 0.03  # same default as the Combos page's Hide thin; a thin leg book never alerts
THIN_SIZE = 50
# Same-game legs aren't independent, so a fair price that multiplies the legs is off; those don't alert,
# and neither do combos over MAX_LEGS legs (MAX_LEGS_LIVE once a game has started) or MAX_LEAGUES leagues
# (the bot's ALERT_* variables change these).
MAX_LEGS = 5
MAX_LEGS_LIVE = 3
MAX_LEAGUES = 2


def key(row: dict) -> str:
    """One alert per combo and side: more fills on the same bet don't ping again."""
    return f"{row['ticker']}|{row['side']}"


def ev_of(row: dict, basis: str = "mid") -> float | None:
    return row["ev_pct_mid"] if basis == "mid" else row["ev_pct_crx"]


# Bet size floor : min EV %. Pregame edges are the ones worth betting, so small pregame bets
# only need a big edge; live edges are mostly lines moving under a stale book, so live bars are higher.
PREGAME_TIERS = "50:100,500:20,1000:10"
LIVE_TIERS = "50:200,500:100,1000:50"


def parse_tiers(text: str) -> list[tuple[float, float]]:
    """'500:20,1000:10' -> [(500, 0.20), (1000, 0.10)]: bets of $500+ need +20% EV, $1,000+ need +10%.
    'off' (or nothing) means no tiers, so nothing of that kind alerts."""
    tiers = []
    for part in text.split(","):
        if part.strip().lower() in ("", "off"):
            continue
        stake, ev = part.split(":")
        tiers.append((float(stake), float(ev) / 100))
    return sorted(tiers)


def min_ev_for(stake: float, tiers: list[tuple[float, float]]) -> float | None:
    """The EV bar for a bet this size (the biggest tier it reaches), or None when it's under every tier."""
    reached = [ev for floor, ev in tiers if stake >= floor]
    return reached[-1] if reached else None


def skipped(row: dict, series: tuple[str, ...] | None = None) -> bool:
    """Has a leg we leave out? By default a crypto, commodity or other financial-market leg
    (financial.is_financial); with `series`, a leg whose ticker starts with one of them (() keeps all)."""
    if series is None:
        return any(financial.is_financial(l["ticker"]) for l in row["leg_detail"])
    return any(l["ticker"].startswith(series) for l in row["leg_detail"]) if series else False


def leagues(row: dict) -> int:
    """How many leagues the legs span, as the app labels them (sports.league_of): NBA + NFL is 2."""
    return len({league_of(l["ticker"])[0] for l in row["leg_detail"]})


def spread_out(row: dict, max_legs: int | None = MAX_LEGS, max_leagues: int | None = MAX_LEAGUES,
               max_legs_live: int | None = MAX_LEGS_LIVE) -> bool:
    """More legs or more leagues than an alert allows (None: no cap). Live combos get the smaller leg cap."""
    caps = [c for c in (max_legs, max_legs_live if is_live(row) else None) if c is not None]
    return bool(caps and row["legs"] > min(caps)) or (max_leagues is not None and leagues(row) > max_leagues)


def pick(rows: list[dict], pregame: list[tuple[float, float]], live: list[tuple[float, float]], sent: set[str],
         basis: str = "mid", skip_series: tuple[str, ...] | None = None, skip_same_game: bool = True,
         max_legs: int | None = MAX_LEGS, max_leagues: int | None = MAX_LEAGUES,
         max_legs_live: int | None = MAX_LEGS_LIVE) -> list[dict]:
    """Bets that clear their size tier's EV bar (pregame or live tiers), not alerted yet, best first.
    Thin leg books (always), same-game legs, combos over max_legs legs (max_legs_live when live) or
    max_leagues leagues, and combos with a crypto, commodity or other financial leg are left out."""
    def wanted(r):
        bar = min_ev_for(r["stake"], live if is_live(r) else pregame)
        return (bar is not None and (ev_of(r, basis) or -1) > bar and key(r) not in sent
                and not thin(r) and not (skip_same_game and r["same_game_legs"])
                and not spread_out(r, max_legs, max_leagues, max_legs_live) and not skipped(r, skip_series))
    return sorted(filter(wanted, rows), key=lambda r: ev_of(r, basis), reverse=True)


def thin(row: dict) -> bool:
    return any(l["spread"] is None or l["spread"] > THIN_SPREAD + 1e-9 or min(l["top_bid_size"], l["top_ask_size"]) < THIN_SIZE
               for l in row["leg_detail"])


TIERS = [(5, 5, "rocket", "🚀"), (1, 5, "moneybag", "💰"), (0.5, 4, "fire", "🔥"), (-1, 3, "green_circle", "🟢")]


def tier(ev: float) -> tuple[int, str, str]:
    """ntfy priority, ntfy emoji tag and emoji: the bigger the edge, the louder the ping."""
    return next((p, tag, emoji) for floor, p, tag, emoji in TIERS if ev >= floor)


def title(row: dict, basis: str = "mid") -> str:
    """'PREGAME +812% EV · $1,250 YES 4-leg combo', the line a lock screen shows."""
    return (f"{'LIVE' if is_live(row) else 'PREGAME'} {ev_of(row, basis) * 100:+.0f}% EV · ${row['stake']:,.0f} "
            f"{row['side'].upper()} {row['legs']}-leg combo")


def summary(row: dict) -> str:
    """'MID +812% · 1.26x · 20u | CRX +640% · 1.28x · 15u': EV, fair multiple and Kelly units at both prices."""
    def part(name, ev, fair, kelly):
        if ev is None or fair is None:
            return f"{name} n/a"
        return f"{name} {ev * 100:+.0f}% · {fmt_mult(fair)} · {fmt_units(units(kelly))}"
    return (f"{part('MID', row['ev_pct_mid'], row['fair_mid'], row['kelly_mid'])} | "
            f"{part('CRX', row['ev_pct_crx'], row['fair_crx'], row['kelly_crx'])}")


def body(row: dict) -> str:
    price = row["price"]
    mult = f"{1 / price:.1f}x" if price else "?"
    notes = [f"{row['minutes_ago']:.0f} min ago"]
    if row["same_game_legs"]:
        notes.append("same game")
    if thin(row):  # the tail bot sends a friend's bets whatever the books
        notes.append("thin leg book")
    if (row["ev_pct_mid"] or 0) > 2:
        notes.append("check the book, +200% edges are often stale")
    return "\n".join([
        summary(row),
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
    """sendMessage JSON: the title and summary lines in bold, the rest of the body, and link buttons under it."""
    emoji = tier(ev_of(row, basis))[2]
    head = f"{emoji} {title(row, basis)}\n{summary(row)}"
    text = f"{emoji} {title(row, basis)}\n{body(row)}"  # body() opens with the summary line
    return {"chat_id": chat_id, "text": text, "entities": [{"type": "bold", "offset": 0, "length": len(head.encode("utf-16-le")) // 2}],
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
