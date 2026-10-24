"""Tail a friend who agreed to it: send each combo bet they place to your phone a few
seconds after it fills.

Kalshi's trade feed has no trader IDs, but every combo bet starts as an RFQ that
carries the requester's code (a stable hash, see rfq.py), and rfq.RfqBook already
puts a code on ~97% of combo bets. So once we know the friend's code, every match
with that code is their bet.

Finding the code takes one or two of their bets, placed while the bot is running:

- "$1,999.76 pays $4,424.51" from a combo on their Positions tab: the payout is the
  number of contracts, which is nearly unique to one bet.
- A combo link or ticker of theirs: the codes that bet that combo are the candidates.
- Their nickname, if Kalshi's social feed shows their trades to us: each of their
  combo trades there has a trade ID, and the RFQ match for that trade has the code.

When several people fit, the next bet you send narrows the candidates down.

scripts/tail_bot.py runs it; this module keeps the matches, the codes and the messages.
"""

from __future__ import annotations

import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from .alerts import buttons, details, payout as payout_line, summary
from .bettors import to_fill
from .client import COMBO_PREFIX
from .engine import taker_fee
from .format import fmt_odds
from .links import bet_url
from .rfq import PLACEHOLDER, Match

TICKER_RE = re.compile(rf"{COMBO_PREFIX}[A-Z0-9._-]+", re.IGNORECASE)
MONEY = r"\$?\s*([\d,]+(?:\.\d{1,2})?)"
PAYOUT_RE = re.compile(rf"(?:{MONEY}\s+)?pays\s+{MONEY}", re.IGNORECASE)

# A matched combo bet as the tail keeps it: (time, code, side, stake in $, trade ids, contracts)
Seen = tuple


def tickers_in(text: str) -> list[str]:
    """Combo tickers in a message: a bare ticker, a markets_by_ticker link or a /combos?marketTicker= link."""
    out = []
    for t in TICKER_RE.findall(text or ""):
        t = t.upper().rstrip(".-")
        if t not in out:
            out.append(t)
    return out


def payouts_in(text: str) -> list[tuple[float | None, float]]:
    """'$1,999.76 pays $4,424.51' (a Positions card) -> [(1999.76, 4424.51)]; the cost is optional."""
    out = []
    for cost, payout in PAYOUT_RE.findall(text or ""):
        out.append((float(cost.replace(",", "")) if cost else None, float(payout.replace(",", ""))))
    return out


def short(code: str) -> str:
    return code[:8]


def costs(contracts: float, stake: float) -> tuple[float, float]:
    """What a bet cost: before and after the taker fee (Kalshi shows either, depending on the screen)."""
    price = stake / contracts if contracts else 0.0
    return stake, stake + taker_fee(price, contracts)


def trades_of(m: Match) -> list[dict]:
    """The matched execution as v2-feed trades, so the Scorer can price it."""
    e = m.execution
    when = datetime.fromtimestamp(e.ts, timezone.utc).isoformat()
    yes = e.price if e.side == "yes" else 1 - e.price
    return [{"trade_id": e.trade_ids[0], "ticker": e.ticker, "taker_side": e.side, "taker_outcome_side": e.side,
             "yes_price_dollars": f"{yes:.4f}", "no_price_dollars": f"{1 - yes:.4f}",
             "count_fp": f"{e.count:.2f}", "created_time": when}]


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts)) + " UTC"


class Tail:
    """Who we follow (codes), the recent combo bets of everyone (to find a code), and what was sent."""

    def __init__(self, label: str = "Friend", keep_hours: float = 6.0, link_trades: bool = True):
        self.label = label
        self.keep = keep_hours * 3600
        self.link_trades = link_trades  # keep trade ids for learn_social (only useful when their trades are public)
        self.codes: dict[str, str] = {}  # code -> how we learned it
        self.candidates: set[str] | None = None  # codes that fit every bet you sent, while there are several
        self.history: dict[str, deque[Seen]] = defaultdict(deque)  # combo ticker -> bets on it, oldest first
        self.by_trade: dict[str, str] = {}  # trade id -> code, for linking social-feed trades
        self.sent: dict[str, float] = {}  # alert keys -> sent at
        self.peeked: dict[str, float] = {}  # combo tickers the friend asked a quote on -> when we said so

    # ---- every matched bet ----
    def add(self, m: Match) -> None:
        e = m.execution
        if not m.creator or m.creator == PLACEHOLDER:
            return
        ids = tuple(e.trade_ids) if self.link_trades else ()
        self.history[e.ticker].append((e.ts, m.creator, e.side, e.count * e.price, ids, e.count))
        for tid in ids:
            self.by_trade[tid] = m.creator

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.keep
        for ticker in list(self.history):
            bets = self.history[ticker]
            while bets and bets[0][0] < cutoff:
                for tid in bets.popleft()[4]:
                    self.by_trade.pop(tid, None)
            if not bets:
                del self.history[ticker]
        self.sent = {k: t for k, t in self.sent.items() if t >= now - 24 * 3600}
        self.peeked = {k: t for k, t in self.peeked.items() if t >= now - 3600}

    def held(self) -> int:
        return sum(len(b) for b in self.history.values())

    def is_friend(self, m: Match) -> bool:
        return m.creator in self.codes

    # ---- finding the code ----
    def adopt(self, code: str, how: str) -> bool:
        new = code not in self.codes
        self.codes[code] = how
        self.candidates = None
        return new

    def _narrow(self, fits: list[tuple[str, str]], what: str) -> str:
        """fits: (code, one line about that code's bet). Keep the codes that fit every bet sent so far."""
        creators = {c for c, _ in fits}
        known = creators & set(self.codes)
        if known:
            return f"That one's already theirs (code {short(next(iter(known)))}). I'm following it."
        before = self.candidates
        restarted = before is not None and not (before & creators)
        self.candidates = creators if before is None or restarted else before & creators
        if len(self.candidates) == 1:
            code = next(iter(self.candidates))
            self.adopt(code, what)
            line = next(l for c, l in fits if c == code)
            return (f"Got it: {line} (code {short(code)}). From now on I'll send you every combo bet from that "
                    f"code. If that wasn't {self.label}, send /forget.")
        lines = [f"{len(self.candidates)} people fit {what}"
                 + (" (none of them fit the last one you sent, so I started over)" if restarted else "")
                 + f". Send me another of {self.label}'s bets to narrow it down. The bets I saw:"]
        lines += [f"• {l} ({short(c)})" for c, l in fits if c in self.candidates][:8]
        return "\n".join(lines)

    def learn_ticker(self, ticker: str) -> str:
        """You sent a combo the friend bet: narrow down whose code is theirs. Returns the reply."""
        bets = list(self.history.get(ticker, ()))
        if not bets:
            return (f"I haven't seen a bet on {ticker} in the last {self.keep / 3600:.0f}h (I only see bets placed "
                    "while I'm running). Send me one of their newer bets.")
        fits = [(b[1], f"{clock(b[0])} ${b[3]:,.0f} {b[2].upper()} on that combo")
                for b in sorted(bets, key=lambda b: -b[0])]
        return self._narrow(fits, f"the combo {ticker}")

    def learn_payout(self, cost: float | None, payout: float) -> str:
        """You sent '$X pays $Y' from their Positions tab: Y is the contracts they hold on one combo
        (all their fills on it together), X what it cost. Narrow down whose code is theirs."""
        totals: dict[tuple, list] = {}
        for ticker, bets in self.history.items():
            for ts, code, side, stake, _, count in bets:
                t = totals.setdefault((ticker, code, side), [0.0, 0.0, ts])
                t[0] += count
                t[1] += stake
                t[2] = max(t[2], ts)
        tol = 0.011  # contracts come in 0.01 steps and each pays $1, so the payout is exact
        fits = []
        for (ticker, code, side), (count, stake, ts) in sorted(totals.items(), key=lambda kv: -kv[1][2]):
            if abs(count - payout) > tol:
                continue
            if cost is not None and not any(abs(c - cost) <= max(0.10, 0.01 * cost) for c in costs(count, stake)):
                continue
            fits.append((code, f"{clock(ts)} ${stake:,.2f} {side.upper()} pays ${count:,.2f} on {ticker}"))
        what = f"${cost:,.2f} pays ${payout:,.2f}" if cost is not None else f"pays ${payout:,.2f}"
        if not fits:
            return (f"I haven't seen a combo bet that {what} in the last {self.keep / 3600:.0f}h. I only see bets "
                    "placed while I'm running, so send me one they place from now on.")
        return self._narrow(fits, what)

    def learn_social(self, trades: list[dict], nickname: str) -> list[str]:
        """Codes behind the friend's own trades in the social feed. Returns the newly learned ones."""
        new = []
        for t in trades:
            f = to_fill(t, nickname)
            if f is None or not f.taker or not f.ticker.startswith(COMBO_PREFIX):
                continue
            code = self.by_trade.get(f.trade_id)
            if code and self.adopt(code, f"{nickname}'s trade {f.trade_id}"):
                new.append(code)
        return new

    # ---- messages ----
    def alert_key(self, ticker: str, side: str, ts: float) -> str:
        return f"{ticker}|{side}|{int(ts // 60)}"

    def head(self, row: dict) -> str:
        return (f"👥 {self.label} bet ${row['stake']:,.0f} {row['side'].upper()} · {row['legs']}-leg combo "
                f"{fmt_odds(row['price'])}")

    def bet_message(self, row: dict, chat_id: str, app_url: str | None = None, lag: float | None = None) -> dict:
        """Telegram sendMessage JSON for one of the friend's bets: what, how big, EV now, and a Bet in Kalshi button."""
        head = self.head(row)
        text = f"{head}\n{summary(row)}\n{payout_line(row)}\n{details(row)}"
        if lag is not None:
            text += f"\n\n(sent {lag:.0f}s after the bet)"
        return {"chat_id": chat_id, "text": text,
                "entities": [{"type": "bold", "offset": 0, "length": len(head.encode("utf-16-le")) // 2}],
                "link_preview_options": {"is_disabled": True},
                "reply_markup": {"inline_keyboard": [[{"text": label, "url": url} for label, url in buttons(row, app_url)]]}}

    def plain_bet_message(self, m: Match, chat_id: str) -> dict:
        """When the bet couldn't be priced: still say what it was, with the link."""
        e = m.execution
        text = (f"👥 {self.label} bet ${e.count * e.price:,.0f} {e.side.upper()} on a combo {fmt_odds(e.price)}\n"
                f"{e.ticker}")
        return {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True},
                "reply_markup": {"inline_keyboard": [[{"text": "Bet in Kalshi", "url": bet_url(e.ticker)}]]}}

    def peek_message(self, ticker: str, chat_id: str) -> dict:
        return {"chat_id": chat_id, "text": f"👀 {self.label} is getting a quote on a combo. Not a bet yet.\n{ticker}",
                "link_preview_options": {"is_disabled": True}, "disable_notification": True,
                "reply_markup": {"inline_keyboard": [[{"text": "Open in Kalshi", "url": bet_url(ticker)}]]}}

    def status(self, book_stats: dict | None = None) -> str:
        lines = [f"Following {self.label}: " + (", ".join(f"{short(c)} (from {how})" for c, how in self.codes.items())
                                               or "no code yet.")]
        if self.candidates:
            lines.append(f"Narrowing down: {len(self.candidates)} candidate codes.")
        lines.append(f"Remembering {self.held():,} combo bets on {len(self.history):,} combos to find codes.")
        if book_stats:
            named = book_stats.get("sure", 0) + book_stats.get("likely", 0)
            lines.append(f"RFQ stream: {book_stats.get('rfqs', 0):,} RFQs heard, {named:,} bets named.")
        return "\n".join(lines)

    # ---- saved between runs ----
    def state(self) -> dict:
        return {"codes": self.codes, "candidates": sorted(self.candidates) if self.candidates else None,
                "sent": self.sent}

    def load(self, data: dict) -> None:
        self.codes.update(data.get("codes") or {})
        self.candidates = set(data["candidates"]) if data.get("candidates") else None
        self.sent.update(data.get("sent") or {})

    def dump_history(self) -> dict:
        """The bets remembered for finding a code, compact, so the next run can pick them up:
        {"codes": [...], "bets": {ticker: [[time, code index, "y"/"n", stake, contracts], ...]}}."""
        codes: dict[str, int] = {}
        bets = {ticker: [[round(ts, 2), codes.setdefault(code, len(codes)), side[0], round(stake, 2), round(count, 2)]
                         for ts, code, side, stake, _, count in seen]
                for ticker, seen in self.history.items() if seen}
        return {"codes": list(codes), "bets": bets}

    def load_history(self, data: dict, now: float | None = None) -> int:
        """Bets an earlier run remembered, still inside the window. Returns how many."""
        cutoff = (time.time() if now is None else now) - self.keep
        codes = [sys.intern(c) for c in data.get("codes") or []]
        n = 0
        for ticker, rows in (data.get("bets") or {}).items():
            old = [(ts, codes[i], "yes" if s == "y" else "no", stake, (), count) for ts, i, s, stake, count in rows if ts >= cutoff]
            if old:
                self.history[ticker] = deque(sorted(old + list(self.history.get(ticker, ())), key=lambda b: b[0]))
                n += len(old)
        return n
