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

from .client import COMBO_PREFIX


def _ts(trade: dict) -> datetime:
    return datetime.fromisoformat(trade["created_time"].replace("Z", "+00:00"))


class Collector:
    def __init__(self, client, max_window_minutes: int = 180, poll_seconds: int = 60,
                 initial_backfill_minutes: int = 60):
        self.client = client
        self.max_window = timedelta(minutes=max_window_minutes)
        self.poll_seconds = poll_seconds
        self.initial_backfill = timedelta(minutes=initial_backfill_minutes)
        self.lock = threading.Lock()
        self.trades: dict[str, dict] = {}  # trade_id -> combo trade
        self.covered_from: datetime | None = None  # oldest time we have complete data for
        self.covered_to: datetime | None = None  # newest time we have complete data for
        self.markets: dict[str, dict] = {}  # combo ticker -> market (legs never change)
        self.last_error: str | None = None
        self.last_poll_seconds: float | None = None
        self.trades_scanned = 0
        self._thread: threading.Thread | None = None

    # ---- collection ----
    def _scan(self, since: datetime, until: datetime | None = None, progress: bool = False) -> None:
        """Pull every trade in [since, until] and keep the combo ones."""
        params_until = until
        for t in self.client.iter_trades(since, max_ts=params_until):
            self.trades_scanned += 1
            if t["ticker"].startswith(COMBO_PREFIX):
                with self.lock:
                    self.trades[t["trade_id"]] = t
            if progress and self.trades_scanned % 1000 == 0:
                # Trades come newest first, so everything after this one is complete.
                with self.lock:
                    self.covered_from = _ts(t)

    def poll(self) -> None:
        start = time.time()
        now = datetime.now(timezone.utc)
        if self.covered_to is None:
            self.covered_to = now
            self._scan(now - self.initial_backfill, progress=True)
            self.covered_from = now - self.initial_backfill
        else:
            # Overlap a few seconds so trades stamped just before the last poll aren't missed.
            self._scan(self.covered_to - timedelta(seconds=5))
            self.covered_to = now
        self._prune(now)
        self.last_poll_seconds = time.time() - start

    def extend_backfill(self) -> None:
        """Walk further back in time until the full window is covered."""
        target = datetime.now(timezone.utc) - self.max_window
        if self.covered_from is None or self.covered_from <= target + timedelta(minutes=1):
            return
        until = self.covered_from
        self._scan(max(target, until - timedelta(minutes=30)), until=until, progress=True)
        self.covered_from = max(target, until - timedelta(minutes=30))

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.max_window
        with self.lock:
            self.trades = {k: t for k, t in self.trades.items() if _ts(t) >= cutoff}
            if self.covered_from and self.covered_from < cutoff:
                self.covered_from = cutoff

    def run_forever(self) -> None:
        while True:
            try:
                self.poll()
                self.extend_backfill()
                self.last_error = None
            except Exception as exc:  # keep collecting through API hiccups
                self.last_error = repr(exc)
            time.sleep(self.poll_seconds)

    def start(self) -> "Collector":
        if self._thread is None:
            self._thread = threading.Thread(target=self.run_forever, daemon=True, name="combo-collector")
            self._thread.start()
        return self

    # ---- reads ----
    def window(self, minutes: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        with self.lock:
            return [t for t in self.trades.values() if _ts(t) >= cutoff]

    def coverage_minutes(self) -> float:
        if self.covered_from is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.covered_from).total_seconds() / 60

    def combo_markets(self, tickers: list[str]) -> dict[str, dict]:
        missing = [t for t in dict.fromkeys(tickers) if t not in self.markets]
        if missing:
            self.markets.update(self.client.get_markets(missing))
        return {t: self.markets[t] for t in tickers if t in self.markets}
