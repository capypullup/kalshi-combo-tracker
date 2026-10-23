"""Score a set of combo trades against the current leg orderbooks."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .engine import _game_key, _parse_time, build_bets, legs_of, score_bet


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

    def get_many(self, keys: list[str], fetch, max_age: float | None = None) -> dict[str, dict]:
        """Return cached values, fetching the missing or expired ones in one call.

        `max_age` tightens the TTL for this call (a refresh tap wants quotes from now).
        """
        now = time.time()
        ttl = self.ttl if max_age is None else min(max_age, self.ttl if self.ttl is not None else max_age)
        with self.lock:
            fresh = {k: v for k, (t, v) in self.data.items()
                     if k in keys and (ttl is None or now - t < ttl)}
        missing = [k for k in dict.fromkeys(keys) if k not in fresh]
        if missing:
            fetched = fetch(missing)
            with self.lock:
                for k, v in fetched.items():
                    self.data[k] = (now, v)
            fresh.update(fetched)
        return fresh


def top_of_book(market: dict) -> dict:
    """A one-level orderbook built from a market's best bid/ask and their sizes.

    Kalshi's public API has no batch orderbook call, so full depth costs one request
    per leg. Top of book comes 100 legs per request from /markets.
    """
    def level(price, size):
        p, s = float(price or 0), float(size or 0)
        return [[f"{p:.4f}", f"{s:.2f}"]] if 0 < p < 1 and s > 0 else []

    yes_ask = market.get("yes_ask_dollars")
    return {
        "yes_dollars": level(market.get("yes_bid_dollars"), market.get("yes_bid_size_fp")),
        "no_dollars": level(1 - float(yes_ask) if yes_ask else 0, market.get("yes_ask_size_fp")),
        "_top_only": True,
    }


class GameStarts:
    """When each event's game starts, from Kalshi's public schedule ("milestones").

    A milestone lists every event for one game, props included. Lookups fall back
    to the game part of the ticker ('KXNBAPTS-26OCT21ATLORL' -> '26OCT21ATLORL').
    """

    def __init__(self, client, ttl: float = 600, back_hours: float = 12, ahead_hours: float = 48):
        self.client = client
        self.ttl = ttl
        self.back = timedelta(hours=back_hours)
        self.ahead = timedelta(hours=ahead_hours)
        self.by_event: dict[str, datetime] = {}
        self.by_game: dict[str, datetime] = {}
        self.fetched_at = 0.0
        self.error: str | None = None

    def refresh(self) -> None:
        if time.time() - self.fetched_at < self.ttl or not hasattr(self.client, "get_milestones"):
            return
        self.fetched_at = time.time()  # also on failure, so a bad response isn't retried on every score
        now = datetime.now(timezone.utc)
        try:
            milestones = self.client.get_milestones(now - self.back, now + self.ahead)
        except Exception as exc:  # keep the last schedule; LIVE falls back to settlement times
            self.error = repr(exc)[:200]
            return
        by_event: dict[str, datetime] = {}
        by_game: dict[str, datetime] = {}
        for m in milestones:
            start = _parse_time(m.get("start_date"))
            if start is None:
                continue
            for ticker in (m.get("related_event_tickers") or []) + (m.get("primary_event_tickers") or []):
                by_event[ticker] = start
                by_game.setdefault(_game_key(ticker), start)
        self.by_event, self.by_game, self.error = by_event, by_game, None

    def __call__(self, event_ticker: str) -> datetime | None:
        return self.by_event.get(event_ticker) or self.by_game.get(_game_key(event_ticker))


class Scorer:
    """Holds the caches that make repeated refreshes cheap."""

    def __init__(self, client, quote_ttl: float = 60, book_ttl: float = 60):
        self.client = client
        self.starts = GameStarts(client)
        self.combo_markets = TTLCache(None)  # a combo's legs never change
        self.events = TTLCache(None)
        self.leg_markets = TTLCache(quote_ttl)  # also carries each leg's top of book
        self.books = TTLCache(book_ttl)  # full depth, fetched only when a bet is opened

    def _leg_markets(self, tickers: list[str], max_age: float | None = None) -> dict[str, dict]:
        return self.leg_markets.get_many(tickers, lambda t: self.client.get_markets(t, batch_size=100), max_age)

    def rescore_with_depth(self, row: dict, trades: list[dict], group: bool = True) -> dict:
        """Re-score one bet using the full orderbook of every leg."""
        bets = [b for b in build_bets(trades, group=group) if b.ticker == row["ticker"] and b.taker_side == row["side"]]
        if not bets:
            return row
        market = self.combo_markets.get_many([row["ticker"]], self.client.get_markets).get(row["ticker"])
        if market is None:
            return row
        legs = legs_of(market)
        leg_tickers = [l["market_ticker"] for l in legs]
        books = self.books.get_many(leg_tickers, self.client.get_orderbooks)
        events = self.events.get_many([l["event_ticker"] for l in legs], self.client.get_events)
        return score_bet(bets[0], market, self._leg_markets(leg_tickers), books, events=events, game_start=self.starts)

    def score(self, trades: list[dict], min_stake: float = 0.0, group: bool = True,
              max_bets: int = 400, quote_age: float | None = None) -> Snapshot:
        now = datetime.now(timezone.utc)
        bets = build_bets(trades, group=group)
        # Filter on size before touching orderbooks; it is what keeps a refresh fast. Keep the
        # newest bets: combo edges disappear within minutes, so recent ones matter most.
        kept = sorted((b for b in bets if b.stake >= min_stake), key=lambda b: b.last_time, reverse=True)
        over_min = len(kept)
        kept = kept[:max_bets]

        errors: list[str] = []
        self.starts.refresh()
        if self.starts.error:
            errors.append(f"game schedule: {self.starts.error}")
        combo_markets = self.combo_markets.get_many([b.ticker for b in kept], self.client.get_markets)
        legs = [l for m in combo_markets.values() for l in legs_of(m)]
        leg_tickers = [l["market_ticker"] for l in legs]
        leg_markets = self._leg_markets(leg_tickers, max_age=quote_age)
        events = self.events.get_many([l["event_ticker"] for l in legs], self.client.get_events)
        books = {t: top_of_book(m) for t, m in leg_markets.items()}

        rows = []
        for bet in kept:
            market = combo_markets.get(bet.ticker)
            if market is None or not legs_of(market):
                errors.append(f"no leg data for {bet.ticker}")
                continue
            rows.append(score_bet(bet, market, leg_markets, books, now=now, events=events, game_start=self.starts))

        return Snapshot(rows, now, len(trades), len(bets), over_min, len(rows), errors)


class Board:
    """Scored snapshots of the views people have open, kept fresh in the background.

    The collector thread calls refresh() after each poll; pages read the latest
    snapshot instantly. Only a view nobody has opened recently is scored while the
    page waits. request() is the refresh button: that view is re-scored from the
    next poll's trades, ahead of the regular pass.
    """

    def __init__(self, scorer: Scorer, trades_for, every: float = 120, forget_after: float = 1800,
                 max_bets: int = 400, default_view: tuple[int, float] = (60, 100.0), stale_request: float = 30,
                 tap_quote_age: float = 10):
        self.scorer = scorer
        self.trades_for = trades_for  # window minutes -> combo trades
        self.every = every
        self.forget_after = forget_after
        self.max_bets = max_bets
        self.stale_request = stale_request  # after this long, serve a refresh without waiting for a poll
        self.tap_quote_age = tap_quote_age  # a refresh re-reads leg quotes older than this
        self.views: dict[tuple[int, float], float] = {default_view: float("inf")}  # view -> last opened
        self.snapshots: dict[tuple[int, float], Snapshot] = {}
        self.as_of: dict[tuple[int, float], float] = {}  # view -> start of the poll its trades came from
        self.failed: dict[tuple[int, float], tuple[float, str]] = {}  # view -> (when, error) of its last try
        self.requests: dict[tuple[int, float], float] = {}  # view -> when someone tapped refresh
        self.trades_as_of: float | None = None  # start of the collector's latest poll (None: no collector)
        self.scored_at = 0.0
        self.lock = threading.Lock()

    def peek(self, window: int, min_stake: float) -> Snapshot | None:
        with self.lock:
            return self.snapshots.get((window, float(min_stake)))

    def get(self, window: int, min_stake: float) -> Snapshot:
        self.touch(window, min_stake)
        return self.peek(window, min_stake) or self._score((window, float(min_stake)))

    def touch(self, window: int, min_stake: float) -> None:
        """Mark a view as open, so the regular pass keeps scoring it."""
        key = (window, float(min_stake))
        with self.lock:
            self.views[key] = max(self.views.get(key, 0.0), time.time())

    def request(self, window: int, min_stake: float) -> float:
        """Ask for this view to be re-scored from trades newer than now. Returns the request time."""
        key, now = (window, float(min_stake)), time.time()
        with self.lock:
            self.views[key] = max(self.views.get(key, 0.0), now)
            self.requests[key] = now
        return now

    def done(self, window: int, min_stake: float, since: float) -> tuple[bool, str | None]:
        """Whether the view has been scored from trades newer than `since`, and the error if that failed."""
        key = (window, float(min_stake))
        with self.lock:
            if self.as_of.get(key, 0.0) >= since:
                return True, None
            when, error = self.failed.get(key, (0.0, None))
        return (True, error) if when >= since else (False, None)

    def refresh(self, force: bool = False, trades_as_of: float | None = None) -> None:
        """Score requested views first, then, every `every` seconds, all open views.

        The collector passes the start time of the poll it just finished, so a
        request is only answered with trades from after the tap.
        """
        if trades_as_of is not None:
            self.trades_as_of = trades_as_of
        now = time.time()
        with self.lock:
            due = [(k, t) for k, t in self.requests.items()
                   if trades_as_of is None or t <= trades_as_of or now - t > self.stale_request]
        for key, t in due:
            self._try(key, fresh=True)
            with self.lock:
                if self.requests.get(key) == t:  # a newer tap waits for the next poll
                    del self.requests[key]
        if not force and now - self.scored_at < self.every:
            return
        with self.lock:
            for key in [k for k, t in self.views.items() if now - t > self.forget_after]:
                del self.views[key]
                for store in (self.snapshots, self.as_of, self.failed, self.requests):
                    store.pop(key, None)
            keys = [k for k in self.views if k not in dict(due)]
        for key in keys:
            if self._waiting(now):
                return  # a tap is waiting on the next poll; this pass resumes after it
            self._try(key)
        self.scored_at = now

    def _waiting(self, now: float) -> bool:
        with self.lock:
            return any(now - t <= self.stale_request for t in self.requests.values())

    def _try(self, key: tuple[int, float], fresh: bool = False) -> None:
        try:
            self._score(key, fresh)
        except Exception as exc:  # keep the worker going; the page shows the error
            with self.lock:
                self.failed[key] = (time.time(), repr(exc)[:200])

    def _score(self, key: tuple[int, float], fresh: bool = False) -> Snapshot:
        as_of = self.trades_as_of or time.time()
        snap = self.scorer.score(self.trades_for(key[0]), min_stake=key[1], max_bets=self.max_bets,
                                 quote_age=self.tap_quote_age if fresh else None)
        with self.lock:
            self.snapshots[key] = snap
            self.as_of[key] = as_of
            self.failed.pop(key, None)
        return snap


def refresh(client, lookback_minutes: int = 60, min_stake: float = 0.0, group: bool = True,
            max_bets: int = 400) -> Snapshot:
    """One-shot: page the trade feed for the window, then score. Fine for short windows."""
    trades = client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes))
    return Scorer(client).score(trades, min_stake=min_stake, group=group, max_bets=max_bets)
