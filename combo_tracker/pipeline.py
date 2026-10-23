"""Score a set of combo trades against the current leg orderbooks."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import financial
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
    over_min_estimated: bool = False  # bets_over_min counts hidden combos past the lookups at the rate seen


class TradesUnavailable(RuntimeError):
    """Kalshi's trade feed isn't answering, so there's nothing newer to score."""


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
        wanted = dict.fromkeys(keys)
        with self.lock:
            fresh = {}
            for k in wanted:
                hit = self.data.get(k)
                if hit is not None and (ttl is None or now - hit[0] < ttl):
                    fresh[k] = hit[1]
        missing = [k for k in wanted if k not in fresh]
        if missing:
            fetched = fetch(missing)
            with self.lock:
                for k, v in fetched.items():
                    self.data[k] = (now, v)
            fresh.update(fetched)
        return fresh

    def prune(self, older_than: float) -> None:
        """Drop entries fetched more than `older_than` seconds ago (they're fetched again if needed)."""
        cutoff = time.time() - older_than
        with self.lock:
            self.data = {k: hit for k, hit in self.data.items() if hit[0] >= cutoff}


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

    PRUNE_EVERY = 600  # seconds between sweeps of entries no window can need any more
    KEEP_LEGS = 4 * 3600  # combos and events: longer than the longest window (3h)
    KEEP_QUOTES = 1800  # leg quotes (the Quote page suggests legs quoted in the last 15 min)

    def __init__(self, client, quote_ttl: float = 60, book_ttl: float = 60, hide=None):
        self.client = client
        self.hide = hide  # e.g. financial.is_financial: combos with a leg it flags are left out
        self.starts = GameStarts(client)
        self.combo_markets = TTLCache(None)  # a combo's legs never change
        self.events = TTLCache(None)
        self.leg_markets = TTLCache(quote_ttl)  # also carries each leg's top of book
        self.books = TTLCache(book_ttl)  # full depth, fetched only when a bet is opened
        self.pruned_at = time.time()

    def prune(self) -> None:
        """Keep memory to what recent windows use: the app and bots run for days."""
        self.pruned_at = time.time()
        self.combo_markets.prune(self.KEEP_LEGS)
        self.events.prune(self.KEEP_LEGS)
        self.leg_markets.prune(max(self.KEEP_QUOTES, 2 * self.leg_markets.ttl))
        self.books.prune(max(600, 2 * self.books.ttl))

    def _events(self, event_tickers: list[str], errors: list[str]) -> dict[str, dict]:
        """Event titles, and each event's Kalshi category (which tells a new crypto series from sports)."""
        try:
            events = self.events.get_many(event_tickers, self.client.get_events)
        except Exception as exc:  # titles are nice to have: fall back to the leg market titles
            if not any(e.startswith("event titles") for e in errors):
                errors.append(f"event titles: {exc!r}"[:200])
            return {}
        financial.learn(events)
        return events

    def _leg_markets(self, tickers: list[str], max_age: float | None = None) -> dict[str, dict]:
        return self.leg_markets.get_many(tickers, lambda t: self.client.get_markets(t, batch_size=200), max_age)

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
        return self.score_bets(build_bets(trades, group=group), len(trades), min_stake, max_bets, quote_age)

    def _hidden(self, market: dict | None) -> bool:
        return bool(self.hide and market and any(self.hide(l["market_ticker"]) for l in legs_of(market)))

    def score_bets(self, bets: list, n_trades: int, min_stake: float = 0.0, max_bets: int = 400,
                   quote_age: float | None = None) -> Snapshot:
        """Score the newest `max_bets` bets of at least `min_stake` (bets from build_bets).

        Board builds a window's bets once and scores every min bet from them.
        """
        now = datetime.now(timezone.utc)
        # Filter on size before touching orderbooks; it is what keeps a refresh fast. Keep the
        # newest bets: combo edges disappear within minutes, so recent ones matter most.
        candidates = sorted((b for b in bets if b.stake >= min_stake), key=lambda b: b.last_time, reverse=True)

        errors: list[str] = []
        if time.time() - self.pruned_at > self.PRUNE_EVERY:
            self.prune()
        self.starts.refresh()
        if self.starts.error:
            errors.append(f"game schedule: {self.starts.error}")
        # Hidden combos (a crypto leg, say) are dropped before the newest-max_bets cut, so the
        # list still shows max_bets bets: look up a little past the cut until it's full. Each
        # chunk's events come first, so a series seen for the first time is classed by its category.
        kept, combo_markets, events, hidden, i = [], {}, {}, 0, 0
        while len(kept) < max_bets and i < len(candidates):
            chunk = candidates[i : i + max_bets - len(kept) + (50 if self.hide else 0)]
            i += len(chunk)
            found = self.combo_markets.get_many([b.ticker for b in chunk], self.client.get_markets)
            combo_markets.update(found)
            events.update(self._events([l["event_ticker"] for b in chunk for l in legs_of(found.get(b.ticker) or {})],
                                       errors))
            for bet in chunk:
                if self._hidden(found.get(bet.ticker)):
                    hidden += 1
                elif len(kept) < max_bets:
                    kept.append(bet)
        # Bets past the lookups weren't classed: count them as hidden at the rate seen so far.
        unseen = len(candidates) - i
        over_min = len(candidates) - hidden - (round(unseen * hidden / i) if i and unseen else 0)

        legs = [l for bet in kept for l in legs_of(combo_markets.get(bet.ticker) or {})]
        leg_markets = self._leg_markets([l["market_ticker"] for l in legs], max_age=quote_age)
        books = {t: top_of_book(m) for t, m in leg_markets.items()}

        rows = []
        for bet in kept:
            market = combo_markets.get(bet.ticker)
            if market is None or not legs_of(market):
                errors.append(f"no leg data for {bet.ticker}")
                continue
            rows.append(score_bet(bet, market, leg_markets, books, now=now, events=events, game_start=self.starts))

        return Snapshot(rows, now, n_trades, len(bets), over_min, len(rows), errors,
                        over_min_estimated=bool(unseen and hidden))


class Board:
    """Scored snapshots of the views people have open, kept fresh in the background.

    The collector thread calls refresh() after each poll; pages read the latest
    snapshot instantly. A view opened for the first time is scored on the next poll
    (or while the page waits, if it asks first). request() is the refresh button: that
    view is re-scored from the next poll's trades, ahead of the regular pass.

    With `wait_for_trades` set (the app), a page that has to score waits up to that many
    seconds for trades from a recent poll: at start-up, or when the collector has been
    paused because nobody had the app open, what it holds is missing or old.
    """

    def __init__(self, scorer: Scorer, trades_for, every: float = 120, forget_after: float = 1800,
                 max_bets: int = 400, default_view: tuple[int, float] = (60, 100.0), stale_request: float = 30,
                 tap_quote_age: float = 10, keep: tuple = (), wait_for_running: float = 30,
                 wait_for_trades: float = 0, trades_current: float = 180):
        self.scorer = scorer
        self.trades_for = trades_for  # window minutes -> combo trades
        self.every = every
        self.forget_after = forget_after
        self.max_bets = max_bets
        self.stale_request = stale_request  # after this long, serve a refresh without waiting for a poll
        self.tap_quote_age = tap_quote_age  # a refresh re-reads leg quotes older than this
        self.wait_for_running = wait_for_running  # a page re-checks a score already running this often
        self.wait_for_trades = wait_for_trades
        self.trades_current = trades_current  # trades from a poll that began this recently are current
        # view -> last opened; the default view and `keep` are always scored
        self.views: dict[tuple[int, float], float] = {(w, float(m)): float("inf") for w, m in (default_view, *keep)}
        self.snapshots: dict[tuple[int, float], Snapshot] = {}
        self.as_of: dict[tuple[int, float], float] = {}  # view -> start of the poll its trades came from
        self.failed: dict[tuple[int, float], tuple[float, str]] = {}  # view -> (when, error) of its last try
        self.requests: dict[tuple[int, float], float] = {}  # view -> when someone tapped refresh
        self.running: dict[tuple[int, float], threading.Event] = {}  # views being scored (taps aside)
        self.trades_as_of: float | None = None  # start of the collector's latest poll (None: no collector)
        self.poll_failed_at = 0.0
        self.scored_at = 0.0
        self.lock = threading.Lock()
        self.polled = threading.Condition(self.lock)  # notified after each poll, good or failed

    def peek(self, window: int, min_stake: float) -> Snapshot | None:
        with self.lock:
            return self.snapshots.get((window, float(min_stake)))

    def get(self, window: int, min_stake: float, max_age: float | None = None) -> Snapshot:
        """The view's snapshot, scoring it now if there is none or it's older than `max_age` seconds.

        If the worker is scoring this view already, this waits for it instead of doing it twice.
        """
        key = (window, float(min_stake))
        self.touch(window, min_stake)
        asked = time.time()
        while True:
            snap = self.peek(*key)
            if snap is not None and (max_age is None or asked - snap.fetched_at.timestamp() <= max_age):
                return snap
            self._wait_for_trades(asked)
            if snap is not None and not self._trades_current():
                raise TradesUnavailable("Kalshi isn't sending trades right now")  # keep the old scores up
            if self._claim(key):
                try:
                    return self._score(key)
                except Exception as exc:  # pages waiting on this score report it rather than retry
                    with self.lock:
                        self.failed[key] = (time.time(), repr(exc)[:200])
                    raise
                finally:
                    self._release(key)
            with self.lock:
                running = self.running.get(key)
            if running is not None:
                running.wait(self.wait_for_running)
                with self.lock:
                    when, error = self.failed.get(key, (0.0, None))
                if error and when >= asked:
                    raise RuntimeError(error)  # the score this page waited on failed: don't pile on

    def _trades_current(self) -> bool:
        return not self.wait_for_trades or (self.trades_as_of is not None
                                            and time.time() - self.trades_as_of <= self.trades_current)

    def _wait_for_trades(self, asked: float) -> None:
        """Wait (up to wait_for_trades seconds after `asked`) for a recent poll, or a failed one."""
        if not self.wait_for_trades:
            return
        with self.polled:
            if self.poll_failed_at > (self.trades_as_of or 0) and asked - self.poll_failed_at < self.trades_current:
                return  # the latest poll failed a moment ago: Kalshi is down, so don't hold the page up
            while ((self.trades_as_of is None or time.time() - self.trades_as_of > self.trades_current)
                   and self.poll_failed_at < asked):
                left = asked + self.wait_for_trades - time.time()
                if left <= 0:
                    return
                self.polled.wait(left)

    def poll_failed(self) -> None:
        """The collector's poll failed: pages waiting for trades go ahead with what there is."""
        with self.polled:
            self.poll_failed_at = time.time()
            self.polled.notify_all()

    def touch(self, window: int, min_stake: float, hold: float | None = None) -> None:
        """Mark a view as open, so the regular pass keeps scoring it (for `hold` seconds if
        given, rather than forget_after, unless it's touched again)."""
        key = (window, float(min_stake))
        at = time.time() if hold is None else time.time() - max(0.0, self.forget_after - hold)
        with self.lock:
            self.views[key] = max(self.views.get(key, 0.0), at)

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
        """Score requested views first, then views nobody has seen scored yet, then, every
        `every` seconds, all open views.

        The collector passes the start time of the poll it just finished, so a
        request is only answered with trades from after the tap.
        """
        if trades_as_of is not None:
            with self.polled:
                self.trades_as_of = trades_as_of
                self.polled.notify_all()
        now = time.time()
        with self.lock:
            due = [(k, t) for k, t in self.requests.items()
                   if trades_as_of is None or t <= trades_as_of or now - t > self.stale_request]
        memo: dict = {}  # each window's bets, built once for this call and then let go
        for key, t in due:
            self._try(key, fresh=True, memo=memo)
            with self.lock:
                # A tap served early (its poll is slow) stays until a poll that began after it.
                if self.requests.get(key) == t and (self.trades_as_of is None or t <= self.trades_as_of):
                    del self.requests[key]
        done = {k for k, _ in due}
        with self.lock:
            cold = sorted(k for k in self.views if k not in self.snapshots and k not in done)
        for key in cold:  # opened since the last pass: score now rather than in up to `every` seconds
            if self._waiting(now):
                return  # a tap is waiting on the next poll; this resumes after it
            self._try(key, memo=memo)
            done.add(key)
        if not force and now - self.scored_at < self.every:
            return
        with self.lock:
            for key in [k for k, t in self.views.items() if now - t > self.forget_after]:
                del self.views[key]
                for store in (self.snapshots, self.as_of, self.failed, self.requests):
                    store.pop(key, None)
            keys = sorted(k for k in self.views if k not in done)  # by window, so each window builds once
        for key in keys:
            if self._waiting(now):
                return  # a tap is waiting on the next poll; this pass resumes after it
            self._try(key, memo=memo)
        self.scored_at = now

    def _waiting(self, now: float) -> bool:
        with self.lock:
            return any(now - t <= self.stale_request for t in self.requests.values())

    def _claim(self, key: tuple[int, float]) -> bool:
        with self.lock:
            if key in self.running:
                return False
            self.running[key] = threading.Event()
            return True

    def _release(self, key: tuple[int, float]) -> None:
        with self.lock:
            event = self.running.pop(key, None)
        if event is not None:
            event.set()  # wakes a page waiting in get(), whether this score worked or not

    def _try(self, key: tuple[int, float], fresh: bool = False, memo: dict | None = None) -> None:
        if not fresh and not self._claim(key):
            return  # a page is scoring this view right now
        try:
            self._score(key, fresh, memo)
        except Exception as exc:  # keep the worker going; the page shows the error
            with self.lock:
                self.failed[key] = (time.time(), repr(exc)[:200])
        finally:
            if not fresh:
                self._release(key)

    def _score(self, key: tuple[int, float], fresh: bool = False, memo: dict | None = None) -> Snapshot:
        as_of = self.trades_as_of or time.time()
        quote_age = self.tap_quote_age if fresh else None
        if hasattr(self.scorer, "score_bets"):
            built = memo.get(key[0]) if memo is not None else None
            if built is None:
                trades = self.trades_for(key[0])
                built = (len(trades), build_bets(trades))
                if memo is not None:
                    memo[key[0]] = built
            snap = self.scorer.score_bets(built[1], built[0], min_stake=key[1], max_bets=self.max_bets,
                                          quote_age=quote_age)
        else:
            snap = self.scorer.score(self.trades_for(key[0]), min_stake=key[1], max_bets=self.max_bets,
                                     quote_age=quote_age)
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
