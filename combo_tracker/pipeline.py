"""End-to-end refresh: pull combo trades, filter by size, fetch leg books, score."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .client import KalshiClient
from .engine import build_bets, legs_of, score_bet


@dataclass
class Snapshot:
    rows: list[dict]
    fetched_at: datetime
    lookback_minutes: int
    combo_trades_seen: int
    bets_seen: int
    bets_scored: int
    errors: list[str] = field(default_factory=list)


def refresh(client: KalshiClient, lookback_minutes: int = 60, min_stake: float = 0.0,
            group: bool = True, max_pages: int = 200) -> Snapshot:
    now = datetime.now(timezone.utc)
    trades = client.combo_trades(now - timedelta(minutes=lookback_minutes), max_pages=max_pages)
    bets = build_bets(trades, group=group)
    # Filter on size before touching orderbooks; it is what keeps a refresh fast.
    kept = [b for b in bets if b.stake >= min_stake]

    errors: list[str] = []
    combo_markets = client.get_markets([b.ticker for b in kept])
    legs = [l for m in combo_markets.values() for l in legs_of(m)]
    leg_tickers = [l["market_ticker"] for l in legs]
    leg_markets = client.get_markets(leg_tickers)
    events = client.get_events([l["event_ticker"] for l in legs])
    books = client.get_orderbooks(leg_tickers)

    rows = []
    for bet in kept:
        market = combo_markets.get(bet.ticker)
        if market is None or not legs_of(market):
            errors.append(f"no leg data for {bet.ticker}")
            continue
        rows.append(score_bet(bet, market, leg_markets, books, now=now, events=events))

    return Snapshot(rows, now, lookback_minutes, len(trades), len(bets), len(rows), errors)
