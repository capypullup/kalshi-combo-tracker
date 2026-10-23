"""Background collector that keeps a rolling window of combo trades in memory.

Kalshi's public trade feed has every trade on the exchange (hundreds per second at
peak), with no way to ask for combos only. Paging an hour of it on every page view
is too slow, so this pulls only the new trades every minute and keeps the combo ones.
It also caches the combo -> legs lookup, which never changes for a given combo.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import activity
from .client import COMBO_PREFIX

THREAD_NAME = "combo-collector"
SLICES = 4  # a long scan pages this many stretches of time at once (Kalshi pages 1,000 trades a call)


def _ts(trade: dict) -> datetime:
    return datetime.fromisoformat(trade["created_time"].replace("Z", "+00:00"))


def _compact(t: dict) -> tuple:
    """Keep only what scoring needs, as a tuple: a few hundred thousand fills fit in ~100MB."""
    side = t.get("taker_outcome_side") or t["taker_side"]
    return (_ts(t).timestamp(), t["trade_id"], t["ticker"], side,
            float(t["yes_price_dollars"]), float(t["count_fp"]))


def _expand(c: tuple) -> dict:
    ts, trade_id, ticker, side, yes_price, count = c
    return {
        "trade_id": trade_id, "ticker": ticker, "taker_side": side, "taker_outcome_side": side,
        "yes_price_dollars": yes_price, "no_price_dollars": 1 - yes_price, "count_fp": count,
        "created_time": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
    }


class Collector:
    def __init__(self, client, max_window_minutes: int = 180, poll_seconds: int = 60,
                 initial_backfill_minutes: int = 60, after_poll=None, after_error=None, slices: int = SLICES):
        self.client = client
        self.max_window = timedelta(minutes=max_window_minutes)
        self.poll_seconds = poll_seconds
        self.initial_backfill = timedelta(minutes=initial_backfill_minutes)
        self.lock = threading.Lock()
        self.trades: dict[str, tuple] = {}  # trade_id -> compact combo trade (see _compact)
        self.covered_from: datetime | None = None  # oldest time we have complete data for
        self.covered_to: datetime | None = None  # newest time we have complete data for
        self.markets: dict[str, dict] = {}  # combo ticker -> market (legs never change)
        self.last_error: str | None = None
        self.last_poll_seconds: float | None = None
        self.after_poll = after_poll  # e.g. pre-score the default view so page loads hit warm caches
        self.after_error = after_error  # e.g. tell pages waiting for this poll to go ahead without it
        self.slices = slices
        self.trades_scanned = 0
        self.poll_started: float | None = None  # when the latest finished poll began
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()  # poll_now() cuts the wait between polls short

    # ---- collection ----
    def _scan(self, since: datetime, until: datetime | None = None, progress: bool = False,
              interruptible: bool = False, slices: int = 1) -> bool:
        """Pull every trade in [since, until] and keep the combo ones.

        Kalshi pages trades newest first, 1,000 a call, so a long scan splits the span into
        `slices` stretches paged at once (the client's rate limit still applies). With
        `progress`, covered_from follows the point back to which every trade is in. Returns
        False if a refresh tap (poll_now) cut an interruptible scan short.
        """
        top = until or datetime.now(timezone.utc)
        step = (top - since) / slices
        spans = [(top - step * k, top - step * (k + 1)) for k in range(slices)]  # newest first
        reached = [hi for hi, _ in spans]  # how far back each stretch has got
        finished = [False] * slices

        def mark() -> None:
            for k, (_, lo) in enumerate(spans):  # complete down to the first stretch still going
                if not finished[k]:
                    self.covered_from = reached[k]
                    return
                self.covered_from = lo

        def scan(k: int) -> bool:
            hi, lo = spans[k]
            # max_ts is whole seconds: go a second past hi so the rest of its second is read too
            # (the newest stretch of an open-ended scan has no upper bound).
            for n, t in enumerate(self.client.iter_trades(lo, max_ts=None if k == 0 and until is None
                                                          else hi + timedelta(seconds=1)), 1):
                if interruptible and self._wake.is_set():
                    return False
                with self.lock:
                    self.trades_scanned += 1
                    if t["ticker"].startswith(COMBO_PREFIX):
                        self.trades[t["trade_id"]] = _compact(t)
                    if progress and n % 1000 == 0:
                        reached[k] = min(reached[k], _ts(t))
                        mark()
            with self.lock:
                finished[k] = True
                if progress:
                    mark()
            return True

        if slices == 1:
            return scan(0)
        with ThreadPoolExecutor(slices, thread_name_prefix=THREAD_NAME) as pool:
            return all(list(pool.map(scan, range(slices))))

    def poll(self) -> None:
        start = time.time()
        now = datetime.fromtimestamp(start, timezone.utc)
        if self.covered_to is None or now - self.covered_to > self.initial_backfill:
            # The first poll, or back after a long pause (nobody had the app open): load the
            # newest trades rather than paging through the whole gap. Until that has worked, the
            # trades held and their coverage stay as they were, so a failed scan is simply redone.
            since = now - self.initial_backfill
            self._scan(since, slices=self.slices)
            with self.lock:
                if self.covered_to is not None:  # what's from before the gap goes; backfill reloads it
                    self.trades = {k: t for k, t in self.trades.items() if t[0] >= since.timestamp()}
                self.covered_from, self.covered_to = since, now
        else:
            # Overlap a few seconds so trades stamped just before the last poll aren't missed.
            gap = now - self.covered_to
            self._scan(self.covered_to - timedelta(seconds=5), slices=self.slices if gap > timedelta(minutes=2) else 1)
            self.covered_to = now
        self._prune(now)
        self.poll_started = start
        self.last_poll_seconds = time.time() - start

    def extend_backfill(self) -> None:
        """Walk further back in time until the full window is covered."""
        target = datetime.now(timezone.utc) - self.max_window
        if self.covered_from is None or self.covered_from <= target + timedelta(minutes=1):
            return
        until = self.covered_from
        since = max(target, until - timedelta(minutes=30))
        # A refresh tap stops this between pages: covered_from then stays where the scan got to.
        if self._scan(since, until=until, progress=True, interruptible=True, slices=self.slices):
            self.covered_from = since

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.max_window
        with self.lock:
            self.trades = {k: t for k, t in self.trades.items() if t[0] >= cutoff.timestamp()}
            if self.covered_from and self.covered_from < cutoff:
                self.covered_from = cutoff

    def run_forever(self) -> None:
        while not self._stop.is_set():
            activity.wait_active(self._stop)  # no Kalshi calls while nobody has the app open
            if self._stop.is_set():
                break
            self._wake.clear()
            try:
                self.poll()
                if self.after_poll:
                    self.after_poll(self)
                if not self._wake.is_set():  # a refresh tap goes before more history
                    self.extend_backfill()
                self.last_error = None
            except Exception as exc:  # keep collecting through API hiccups
                self.last_error = repr(exc)
                if self.after_error:
                    self.after_error(self)
            self._wake.wait(self.poll_seconds)

    def poll_now(self) -> None:
        """Poll as soon as the current step finishes instead of waiting out the minute."""
        self._wake.set()

    def start(self) -> "Collector":
        if self._thread is None:
            self._thread = threading.Thread(target=self.run_forever, daemon=True, name=THREAD_NAME)
            self._thread.stop = self.stop  # lets a reloaded app stop collectors built from old code
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ---- reads ----
    def window(self, minutes: int) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp()
        with self.lock:
            recent = [t for t in self.trades.values() if t[0] >= cutoff]
        return [_expand(t) for t in recent]

    def coverage_minutes(self) -> float:
        if self.covered_from is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.covered_from).total_seconds() / 60

    def combo_markets(self, tickers: list[str]) -> dict[str, dict]:
        missing = [t for t in dict.fromkeys(tickers) if t not in self.markets]
        if missing:
            self.markets.update(self.client.get_markets(missing))
        return {t: self.markets[t] for t in tickers if t in self.markets}
