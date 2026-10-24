"""Phone alerts for big +EV combo bets, sent by a Telegram bot or through ntfy (https://ntfy.sh).

Both are free. Telegram has no daily cap; ntfy needs no account but its free tier
allows about 250 messages a day. scripts/alert_bot.py runs the loop; this module
picks the bets and writes the messages.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, replace

import requests

from . import financial
from .format import bet_card, fmt_mult, fmt_odds, fmt_units, units
from .links import bet_url
from .sports import league_of
from .view import is_live

NTFY_URL = "https://ntfy.sh"
TELEGRAM_URL = "https://api.telegram.org"
THIN_SPREAD = 0.03  # same default as the Combos page's Hide thin
THIN_SIZE = 50
# Same-game legs aren't independent, so a fair price that multiplies the legs is off; those don't alert,
# and neither do combos over MAX_LEGS legs (MAX_LEGS_LIVE once a game has started) or MAX_LEAGUES leagues.
# These are the defaults: the bot's ALERT_* variables and the app's Alerts tool (Settings below) change them.
MAX_LEGS = 5
MAX_LEGS_LIVE = 3
MAX_LEAGUES = 2
# Where the app's Alerts tool saves its settings; the bot re-reads the file every minute.
SETTINGS_BRANCH = "alert-settings"
SETTINGS_FILE = "alert_settings.json"


def key(row: dict) -> str:
    """One alert per combo and side: more fills on the same bet don't ping again."""
    return f"{row['ticker']}|{row['side']}"


def ev_of(row: dict, basis: str = "mid") -> float | None:
    return row["ev_pct_mid"] if basis == "mid" else row["ev_pct_crx"]


# Bet size floor : min EV %. Pregame edges are the ones worth betting, so small pregame bets
# only need a big edge; live edges are mostly lines moving under a stale book, so live alerts start at $1k
# and need +100% (+50% from $5k).
PREGAME_TIERS = "50:100,500:20,1000:10"
LIVE_TIERS = "1000:100,5000:50"


def parse_tiers(text: str) -> list[tuple[float, float]]:
    """'500:20,1000:10' -> [(500, 0.20), (1000, 0.10)]: bets of $500+ need +20% EV, $1,000+ need +10%.
    'off' (or nothing) means no tiers, so nothing of that kind alerts."""
    tiers = []
    for part in text.split(","):
        if part.strip().lower() in ("", "off"):
            continue
        stake, ev = part.split(":")
        tiers.append((float(stake), round(float(ev) / 100, 6)))  # rounded, so a saved tier reads back the same
    if any(not (0 < stake < math.inf and math.isfinite(ev)) for stake, ev in tiers):
        raise ValueError(f"a tier needs a bet size over $0 and an EV %: {text!r}")
    return sorted(tiers)


def tiers_text(tiers: list[tuple[float, float]]) -> str:
    """[(500, 0.20), (1000, 0.10)] -> '500:20,1000:10' (parse_tiers' format), 'off' for none."""
    return ",".join(f"{stake:.15g}:{round(ev * 100, 4):.15g}" for stake, ev in sorted(tiers)) or "off"


def tiers_words(tiers: list[tuple[float, float]]) -> str:
    """'$500+ at +20%, $1,000+ at +10%', or 'off'."""
    return ", ".join(f"${stake:,.0f}+ at {ev * 100:+.0f}%" for stake, ev in sorted(tiers)) or "off"


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


def _cap(value, name: str) -> int | None:
    """A leg or league cap from the app's file or an ALERT_* variable: a whole number from 1, or None / 'off'."""
    if value is None or (isinstance(value, str) and value.strip().lower() == "off"):
        return None
    if isinstance(value, bool) or not float(value).is_integer() or int(float(value)) < 1:
        raise ValueError(f"{name} must be a whole number from 1, or off: {value!r}")
    return int(float(value))


def _flag(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("1", "0", "true", "false", "on", "off"):
        return value.strip().lower() in ("1", "true", "on")
    raise ValueError(f"{name} must be on or off: {value!r}")


def _amount(value, name: str) -> float:
    if isinstance(value, bool) or not 0 <= float(value) < math.inf:
        raise ValueError(f"{name} must be a number from 0: {value!r}")
    return float(value)


@dataclass
class Settings:
    """Everything that decides which bets alert. The bot starts from these defaults and its ALERT_*
    variables, then takes whatever the app's Alerts tool saved (SETTINGS_FILE), re-read every minute."""
    pregame: list[tuple[float, float]] = field(default_factory=lambda: parse_tiers(PREGAME_TIERS))
    live: list[tuple[float, float]] = field(default_factory=lambda: parse_tiers(LIVE_TIERS))
    basis: str = "mid"  # the EV the tiers compare: "mid" or "crx" (crossing the spread)
    skip_thin: bool = True
    thin_spread: float = THIN_SPREAD  # dollars: a leg spread over this is thin
    thin_size: float = THIN_SIZE  # contracts at the top of a leg book under this is thin
    skip_same_game: bool = True
    max_legs: int | None = MAX_LEGS  # None: no cap
    max_legs_live: int | None = MAX_LEGS_LIVE  # once a game has started
    max_leagues: int | None = MAX_LEAGUES
    # Combos with a crypto, commodity or other financial leg never alert (financial.is_financial).

    def to_dict(self) -> dict:
        """The JSON the app saves (tiers in the ALERT_TIERS_* format, the thin spread in cents)."""
        return {"pregame": tiers_text(self.pregame), "live": tiers_text(self.live), "basis": self.basis,
                "skip_thin": self.skip_thin, "thin_spread_cents": round(self.thin_spread * 100, 4),
                "thin_size": self.thin_size, "skip_same_game": self.skip_same_game, "max_legs": self.max_legs,
                "max_legs_live": self.max_legs_live, "max_leagues": self.max_leagues}

    @classmethod
    def from_dict(cls, data: dict, base: Settings | None = None) -> Settings:
        """`base` (defaults if None) changed by the keys `data` has; other keys are ignored.
        Raises ValueError for a value that doesn't make sense, so a bad file changes nothing."""
        s = replace(base or cls())
        try:
            for name in ("pregame", "live"):
                if name in data:
                    setattr(s, name, parse_tiers(str(data[name])))
            if "basis" in data:
                if str(data["basis"]).lower() not in ("mid", "crx"):
                    raise ValueError(f"basis must be mid or crx: {data['basis']!r}")
                s.basis = str(data["basis"]).lower()
            for name in ("skip_thin", "skip_same_game"):
                if name in data:
                    setattr(s, name, _flag(data[name], name))
            if "thin_spread_cents" in data:
                s.thin_spread = round(_amount(data["thin_spread_cents"], "thin_spread_cents") / 100, 6)
            if "thin_size" in data:
                s.thin_size = _amount(data["thin_size"], "thin_size")
            for name in ("max_legs", "max_legs_live", "max_leagues"):
                if name in data:
                    setattr(s, name, _cap(data[name], name))
        except (TypeError, ValueError) as exc:  # float("x"), a tier without ":"
            raise ValueError(str(exc)) from None
        return s

    @classmethod
    def from_env(cls, env) -> Settings:
        """The bot's ALERT_* variables over the defaults (blank or unset keeps a default)."""
        names = {"ALERT_TIERS_PREGAME": "pregame", "ALERT_TIERS_LIVE": "live", "ALERT_EV_BASIS": "basis",
                 "ALERT_SKIP_SAME_GAME": "skip_same_game", "ALERT_MAX_LEGS": "max_legs",
                 "ALERT_MAX_LEGS_LIVE": "max_legs_live", "ALERT_MAX_LEAGUES": "max_leagues"}
        data = {key: (env.get(var) or "").strip() for var, key in names.items() if (env.get(var) or "").strip()}
        if "skip_same_game" in data:
            data["skip_same_game"] = data["skip_same_game"] != "0"
        return cls.from_dict(data)

    def min_bet(self) -> float | None:
        """The smallest bet any tier alerts, or None when both kinds are off."""
        return min((stake for stake, _ in self.pregame + self.live), default=None)

    def describe(self) -> str:
        """One line for the bot's log: 'EV MID pregame: $50+ at +100%, ... · live: ... · no thin books · ...'."""
        on = [("no thin books" + (f" (over {self.thin_spread * 100:g}¢ or under {self.thin_size:g})"
                                  if (self.thin_spread, self.thin_size) != (THIN_SPREAD, THIN_SIZE) else ""), self.skip_thin),
              ("no same-game legs", self.skip_same_game),
              (f"up to {self.max_legs} legs", self.max_legs is not None),
              (f"up to {self.max_legs_live} legs live", self.max_legs_live is not None),
              (f"up to {self.max_leagues} leagues", self.max_leagues is not None)]
        return " · ".join([f"EV {self.basis.upper()} pregame: {tiers_words(self.pregame)}",
                           f"live: {tiers_words(self.live)}"] + [text for text, keep in on if keep])


class AppSettings:
    """What the app's Alerts tool saved (a ghfile.GitHubFile), over `base`, re-read on every get().
    A file that can't be read or doesn't make sense keeps the settings in force, and says so once in the log;
    no file means `base`."""

    def __init__(self, store, base: Settings, log=print):
        self.store, self.base, self.log = store, base, log
        self.settings, self.seen, self.failing = base, None, False

    def get(self) -> Settings:
        try:
            data, sha = self.store.read()
        except Exception as exc:
            if not self.failing:
                self.log(f"couldn't read the app's alert settings ({exc!r}); keeping these")
            self.failing = True
            return self.settings
        self.failing = False
        mark = sha or json.dumps(data, sort_keys=True)
        if mark == self.seen:
            return self.settings
        self.seen = mark
        try:
            self.settings = Settings.from_dict(data, self.base) if isinstance(data, dict) else self.base
        except ValueError as exc:
            self.log(f"the app's alert settings don't make sense ({exc}); keeping these")
            return self.settings
        self.log(f"{'settings from the app' if isinstance(data, dict) else 'no app settings'}: {self.settings.describe()}")
        return self.settings


# Why a bet that clears its EV bar doesn't alert: filtered_by() gives these keys.
REASONS = {"thin": "thin leg book", "same_game": "same-game legs", "legs": "too many legs",
           "legs_live": "too many legs for live", "leagues": "too many leagues", "financial": "financial leg"}


def bar(row: dict, s: Settings) -> float | None:
    """The EV this bet must beat (its size tier, pregame or live), or None when no tier takes it."""
    return min_ev_for(row["stake"], s.live if is_live(row) else s.pregame)


def clears(row: dict, s: Settings) -> bool:
    b = bar(row, s)
    return b is not None and (ev_of(row, s.basis) or -1) > b


def filtered_by(row: dict, s: Settings) -> list[str]:
    """The filters (REASONS keys) that leave this bet out; empty when none does."""
    out = []
    if s.skip_thin and thin(row, s.thin_spread, s.thin_size):
        out.append("thin")
    if s.skip_same_game and row["same_game_legs"]:
        out.append("same_game")
    if s.max_legs is not None and row["legs"] > s.max_legs:
        out.append("legs")
    if s.max_legs_live is not None and row["legs"] > s.max_legs_live and is_live(row):
        out.append("legs_live")
    if s.max_leagues is not None and leagues(row) > s.max_leagues:
        out.append("leagues")
    if skipped(row):  # always
        out.append("financial")
    return out


def pick(rows: list[dict], s: Settings, sent: set[str]) -> list[dict]:
    """Bets that clear their size tier's EV bar, pass every filter that's on and haven't alerted yet, best first."""
    hits = [r for r in rows if clears(r, s) and key(r) not in sent and not filtered_by(r, s)]
    return sorted(hits, key=lambda r: ev_of(r, s.basis), reverse=True)


def thin(row: dict, spread: float = THIN_SPREAD, size: float = THIN_SIZE) -> bool:
    """A leg with no quote, a spread over `spread` or under `size` contracts at the top of its book."""
    return any(l["spread"] is None or l["spread"] > spread + 1e-9 or min(l["top_bid_size"], l["top_ask_size"]) < size
               for l in row["leg_detail"])


TIERS = [(5, 5, "rocket", "🚀"), (1, 5, "moneybag", "💰"), (0.5, 4, "fire", "🔥"), (-1, 3, "green_circle", "🟢")]


def tier(ev: float) -> tuple[int, str, str]:
    """ntfy priority, ntfy emoji tag and emoji: the bigger the edge, the louder the ping."""
    return next((p, tag, emoji) for floor, p, tag, emoji in TIERS if ev >= floor)


def title(row: dict, basis: str = "mid") -> str:
    """'PREGAME +812% EV', the first line a lock screen shows."""
    return f"{'LIVE' if is_live(row) else 'PREGAME'} {ev_of(row, basis) * 100:+.0f}% EV"


def payout(row: dict) -> str:
    """'$1,250 @ 13.0x (+1200) → $16,250': bet, multiplier, American odds and what it pays."""
    price = row["price"]
    mult = f"{1 / price:.1f}x" if price else "?"
    return f"${row['stake']:,.0f} @ {mult} ({fmt_odds(price)}) → ${row['max_payout']:,.0f}"


def bet_line(row: dict) -> str:
    """'$1,250 @ 13.0x (+1200) → $16,250 4-leg combo', the line under the title ('NO 4-leg combo' for a NO bet)."""
    return f"{payout(row)} {'NO ' if row['side'] == 'no' else ''}{row['legs']}-leg combo"


def summary(row: dict) -> str:
    """'MID +812% · 1.26x · 20u | CRX +640% · 1.28x · 15u': EV, fair multiple and Kelly units at both prices."""
    def part(name, ev, fair, kelly):
        if ev is None or fair is None:
            return f"{name} n/a"
        return f"{name} {ev * 100:+.0f}% · {fmt_mult(fair)} · {fmt_units(units(kelly))}"
    return (f"{part('MID', row['ev_pct_mid'], row['fair_mid'], row['kelly_mid'])} | "
            f"{part('CRX', row['ev_pct_crx'], row['fair_crx'], row['kelly_crx'])}")


def details(row: dict, thin_at: tuple[float, float] = (THIN_SPREAD, THIN_SIZE)) -> str:
    """How long ago, warnings and the bet card: the lines under an alert's (or a tail message's) headline.
    `thin_at`: the spread and top size a leg book counts as thin below (the bot passes its settings')."""
    notes = [f"{row['minutes_ago']:.0f} min ago"]
    if row["same_game_legs"]:
        notes.append("same game")
    if thin(row, *thin_at):  # the tail bot sends a friend's bets whatever the books
        notes.append("thin leg book")
    if (row["ev_pct_mid"] or 0) > 2:
        notes.append("check the book, +200% edges are often stale")
    return "\n".join([" · ".join(notes), "", bet_card(row)])


def body(row: dict, thin_at: tuple[float, float] = (THIN_SPREAD, THIN_SIZE)) -> str:
    """Everything under the title: the bet line, the MID/CRX summary, then details()."""
    return "\n".join([bet_line(row), summary(row), details(row, thin_at)])


def buttons(row: dict, app_url: str | None = None) -> list[tuple[str, str]]:
    out = [("Bet in Kalshi", bet_url(row["ticker"]))]
    if app_url:
        q = f"t={row['ticker']}&side={row['side']}&p={row['price']:.4f}&n={row['contracts']:.0f}"
        out.append(("Quote it", f"{app_url.rstrip('/')}/quote?{q}"))
    return out


def message(row: dict, topic: str, basis: str = "mid", app_url: str | None = None,
            thin_at: tuple[float, float] = (THIN_SPREAD, THIN_SIZE)) -> dict:
    """The JSON ntfy takes on its root URL (title, body, tap action, buttons)."""
    priority, tag, _ = tier(ev_of(row, basis))
    return {"topic": topic, "title": title(row, basis), "message": body(row, thin_at), "priority": priority,
            "tags": [tag], "click": bet_url(row["ticker"]),
            "actions": [{"action": "view", "label": label, "url": url} for label, url in buttons(row, app_url)]}


def telegram_message(row: dict, chat_id: str, basis: str = "mid", app_url: str | None = None,
                     thin_at: tuple[float, float] = (THIN_SPREAD, THIN_SIZE)) -> dict:
    """sendMessage JSON: the title, bet and summary lines in bold, the rest of the body, and link buttons under it."""
    emoji = tier(ev_of(row, basis))[2]
    head = f"{emoji} {title(row, basis)}\n{bet_line(row)}\n{summary(row)}"
    text = f"{emoji} {title(row, basis)}\n{body(row, thin_at)}"  # body() opens with the bet and summary lines
    return {"chat_id": chat_id, "text": text, "entities": [{"type": "bold", "offset": 0, "length": len(head.encode("utf-16-le")) // 2}],
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": [[{"text": label, "url": url} for label, url in buttons(row, app_url)]]}}


class Ntfy:
    def __init__(self, topic: str, session=requests):
        self.topic, self.session = topic, session

    def send(self, row: dict, basis: str = "mid", app_url: str | None = None,
             thin_at: tuple[float, float] = (THIN_SPREAD, THIN_SIZE)) -> None:
        self.session.post(NTFY_URL, json=message(row, self.topic, basis, app_url, thin_at), timeout=15).raise_for_status()


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

    def send(self, row: dict, basis: str = "mid", app_url: str | None = None,
             thin_at: tuple[float, float] = (THIN_SPREAD, THIN_SIZE)) -> None:
        self.post(telegram_message(row, self.chat_id, basis, app_url, thin_at))
