"""Thin wrapper around Kalshi's public (no-key) REST API."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterator

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
COMBO_PREFIX = "KXMVE"


class RateLimiter:
    """Spaces requests out to at most `rate` per second across all threads."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if wait > 0:
            time.sleep(wait)

    def pause(self, seconds: float) -> None:
        """Hold every caller back, e.g. after a 429."""
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 15.0, max_workers: int = 4,
                 rate_per_sec: float = 10.0):
        self.base_url = base_url
        self.timeout = timeout
        self.max_workers = max_workers
        self.limiter = RateLimiter(rate_per_sec)
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        self.batch_orderbooks_ok: bool | None = None  # learned on first try
        self.requests_made = 0

    def _get(self, path: str, params: dict | None = None, retries: int = 5) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(retries + 1):
            self.limiter.wait()
            self.requests_made += 1
            resp = self.session.get(url, params=params, timeout=self.timeout)
            # 429 = rate limited; back off and retry. 5xx are usually transient too.
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    resp.raise_for_status()
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = min(1.0 * 2**attempt, 16)
                self.limiter.pause(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def iter_trades(self, min_ts: datetime, max_ts: datetime | None = None, max_pages: int = 10_000) -> Iterator[dict]:
        """Yield every trade (newest first) created at or after min_ts."""
        params = {"limit": 1000, "min_ts": int(min_ts.timestamp())}
        if max_ts is not None:
            params["max_ts"] = int(max_ts.timestamp())
        for _ in range(max_pages):
            data = self._get("/markets/trades", params)
            yield from data.get("trades", [])
            cursor = data.get("cursor")
            if not cursor or not data.get("trades"):
                return
            params["cursor"] = cursor

    def combo_trades(self, min_ts: datetime, max_pages: int = 10_000) -> list[dict]:
        return [t for t in self.iter_trades(min_ts, max_pages=max_pages) if t["ticker"].startswith(COMBO_PREFIX)]

    def _parallel(self, fn, items):
        with ThreadPoolExecutor(self.max_workers) as pool:
            return list(pool.map(fn, items))

    def get_markets(self, tickers: list[str], batch_size: int = 50) -> dict[str, dict]:
        """Fetch market objects for many tickers, keyed by ticker."""
        unique = list(dict.fromkeys(tickers))
        batches = [unique[i : i + batch_size] for i in range(0, len(unique), batch_size)]

        def fetch(batch: list[str]) -> list[dict]:
            data = self._get("/markets", {"tickers": ",".join(batch), "limit": len(batch)})
            return data.get("markets", [])

        return {m["ticker"]: m for markets in self._parallel(fetch, batches) for m in markets}

    def get_events(self, event_tickers: list[str]) -> dict[str, dict]:
        """Fetch event objects (for titles like 'Cleveland vs Tampa Bay: Strikeouts'), keyed by ticker."""
        unique = list(dict.fromkeys(event_tickers))

        def fetch(ticker: str) -> tuple[str, dict]:
            try:
                return ticker, self._get(f"/events/{ticker}", {"with_nested_markets": "false"}).get("event", {})
            except requests.HTTPError:
                return ticker, {}

        return dict(self._parallel(fetch, unique))

    def get_orderbooks(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch the raw orderbook for each ticker, keyed by ticker.

        Uses the batch endpoint (100 tickers per call) when it works without a key,
        otherwise one call per ticker.
        """
        unique = list(dict.fromkeys(tickers))
        fetched_at = datetime.now(timezone.utc)
        out: dict[str, dict] = {}
        if self.batch_orderbooks_ok is not False and unique:
            try:
                for i in range(0, len(unique), 100):
                    data = self._get("/markets/orderbooks", {"tickers": ",".join(unique[i : i + 100])})
                    for ob in data.get("orderbooks", []):
                        book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
                        out[ob["ticker"]] = {**book, "_fetched_at": fetched_at}
                if out:
                    self.batch_orderbooks_ok = True
                    return out
                self.batch_orderbooks_ok = False  # unexpected shape; fall back
            except requests.HTTPError:
                self.batch_orderbooks_ok = False
                out.clear()

        def fetch(ticker: str) -> tuple[str, dict]:
            data = self._get(f"/markets/{ticker}/orderbook")
            return ticker, data.get("orderbook_fp") or data.get("orderbook") or {}

        for ticker, book in self._parallel(fetch, unique):
            out[ticker] = {**book, "_fetched_at": fetched_at}
        return out
