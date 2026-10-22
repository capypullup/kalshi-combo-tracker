"""Score a set of combo trades against the current leg orderbooks."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .engine import build_bets, legs_of, score_bet


@dataclass
class Snapshot:
    rows: list[dict]
    fetched_at: datetime
    combo_trades_seen: int
    bets_seen: int
    bets_over_min: int
    bets_scored: int
    errors: list[str] = field(default_factory=list)


class TTLCache:
    def __init__(self, ttl_seconds: float | None):
        self.ttl = ttl_seconds
        self.data: dict[str, tuple[float, dict]] = {}
        self.lock = threading.Lock()

    def get_many(self, keys: list[str], fetch) -> dict[str, dict]:
        """Return cached values, fetching the missing or expired ones in one call."""
        now = time.time()
        with self.lock:
            fresh = {k: v for k, (t, v) in self.data.items()
                     if k in keys and (self.ttl is None or now - t < self.ttl)}
        missing = [k for k in dict.fromkeys(keys) if k not in fresh]
        if missing:
            fetched = fetch(missing)
            with self.lock:
                for k, v in fetched.items():
                    self.data[k] = (now, v)
            fresh.update(fetched)
        return fresh


class Scorer:
    """Holds the caches that make repeated refreshes cheap."""

    def __init__(self, client, book_ttl: float = 60, leg_ttl: float = 600):
        self.client = client
        self.combo_markets = TTLCache(None)  # a combo's legs never change
        self.events = TTLCache(None)
        self.leg_markets = TTLCache(leg_ttl)
        self.books = TTLCache(book_ttl)

    def score(self, trades: list[dict], min_stake: float = 0.0, group: bool = True,
              max_bets: int = 400) -> Snapshot:
        now = datetime.now(timezone.utc)
        bets = build_bets(trades, group=group)
        # Filter on size before touching orderbooks; it is what keeps a refresh fast.
        kept = sorted((b for b in bets if b.stake >= min_stake), key=lambda b: -b.stake)
        over_min = len(kept)
        kept = kept[:max_bets]

        errors: list[str] = []
        combo_markets = self.combo_markets.get_many([b.ticker for b in kept], self.client.get_markets)
        legs = [l for m in combo_markets.values() for l in legs_of(m)]
        leg_tickers = [l["market_ticker"] for l in legs]
        leg_markets = self.leg_markets.get_many(leg_tickers, self.client.get_markets)
        events = self.events.get_many([l["event_ticker"] for l in legs], self.client.get_events)
        books = self.books.get_many(leg_tickers, self.client.get_orderbooks)

        rows = []
        for bet in kept:
            market = combo_markets.get(bet.ticker)
            if market is None or not legs_of(market):
                errors.append(f"no leg data for {bet.ticker}")
                continue
            rows.append(score_bet(bet, market, leg_markets, books, now=now, events=events))

        return Snapshot(rows, now, len(trades), len(bets), over_min, len(rows), errors)


def refresh(client, lookback_minutes: int = 60, min_stake: float = 0.0, group: bool = True,
            max_bets: int = 400) -> Snapshot:
    """One-shot: page the trade feed for the window, then score. Fine for short windows."""
    trades = client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes))
    return Scorer(client).score(trades, min_stake=min_stake, group=group, max_bets=max_bets)
