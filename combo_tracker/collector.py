"""Background collector that keeps a rolling window of combo trades in memory.

Kalshi's public trade feed has every trade on the exchange (hundreds per second at
peak), with no way to ask for combos only. Paging an hour of it on every page view
is too slow, so this pulls only the new trades every minute and keeps the combo ones.
It also caches the combo -> legs lookup, which never changes for a given combo.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from . import activity
from .client import COMBO_PREFIX

THREAD_NAME = "combo-collector"


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
                 initial_backfill_minutes: int = 60, after_poll=None):
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
        self.trades_scanned = 0
        self.poll_started: float | None = None  # when the latest finished poll began
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()  # poll_now() cuts the wait between polls short

    # ---- collection ----
    def _scan(self, since: datetime, until: datetime | None = None, progress: bool = False,
              interruptible: bool = False) -> bool:
        """Pull every trade in [since, until] and keep the combo ones. Returns False if a refresh
        tap (poll_now) cut an interruptible scan short."""
        params_until = until
        for t in self.client.iter_trades(since, max_ts=params_until):
            if interruptible and self._wake.is_set():
                return False
            self.trades_scanned += 1
            if t["ticker"].startswith(COMBO_PREFIX):
                with self.lock:
                    self.trades[t["trade_id"]] = _compact(t)
            if progress and self.trades_scanned % 1000 == 0:
                # Trades come newest first, so everything after this one is complete.
                with self.lock:
                    self.covered_from = _ts(t)
        return True

    def poll(self) -> None:
        start = time.time()
        now = datetime.fromtimestamp(start, timezone.utc)
        if self.covered_to is not None and now - self.covered_to > self.initial_backfill:
            # Back after a long pause (nobody had the app open): start over from the newest
            # trades rather than paging through the whole gap at once.
            with self.lock:
                self.trades.clear()
                self.covered_from = self.covered_to = None
        if self.covered_to is None:
            self.covered_to = now
            self._scan(now - self.initial_backfill, progress=True)
            self.covered_from = now - self.initial_backfill
        else:
            # Overlap a few seconds so trades stamped just before the last poll aren't missed.
            self._scan(self.covered_to - timedelta(seconds=5))
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
        # A refresh tap stops this between pages: covered_from then stays where the scan got to.
        if self._scan(max(target, until - timedelta(minutes=30)), until=until, progress=True, interruptible=True):
            self.covered_from = max(target, until - timedelta(minutes=30))

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
