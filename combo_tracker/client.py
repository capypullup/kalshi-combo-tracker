"""Thin wrapper around Kalshi's public (no-key) REST API."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterator

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
COMBO_PREFIX = "KXMVE"


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 15.0, max_workers: int = 8):
        self.base_url = base_url
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

    def _get(self, path: str, params: dict | None = None, retries: int = 4) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(retries + 1):
            resp = self.session.get(url, params=params, timeout=self.timeout)
            # 429 = rate limited; back off and retry. 5xx are usually transient too.
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    resp.raise_for_status()
                time.sleep(0.5 * 2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def iter_trades(self, min_ts: datetime, max_ts: datetime | None = None, max_pages: int = 200) -> Iterator[dict]:
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

    def combo_trades(self, min_ts: datetime, max_pages: int = 200) -> list[dict]:
        return [t for t in self.iter_trades(min_ts, max_pages=max_pages) if t["ticker"].startswith(COMBO_PREFIX)]

    def get_markets(self, tickers: list[str], batch_size: int = 50) -> dict[str, dict]:
        """Fetch market objects for many tickers, keyed by ticker."""
        unique = list(dict.fromkeys(tickers))
        batches = [unique[i : i + batch_size] for i in range(0, len(unique), batch_size)]

        def fetch(batch: list[str]) -> list[dict]:
            data = self._get("/markets", {"tickers": ",".join(batch), "limit": len(batch)})
            return data.get("markets", [])

        out: dict[str, dict] = {}
        with ThreadPoolExecutor(self.max_workers) as pool:
            for markets in pool.map(fetch, batches):
                for m in markets:
                    out[m["ticker"]] = m
        return out

    def get_events(self, event_tickers: list[str]) -> dict[str, dict]:
        """Fetch event objects (for titles like 'Cleveland vs Tampa Bay: Strikeouts'), keyed by ticker."""
        unique = list(dict.fromkeys(event_tickers))

        def fetch(ticker: str) -> tuple[str, dict]:
            try:
                return ticker, self._get(f"/events/{ticker}", {"with_nested_markets": "false"}).get("event", {})
            except requests.HTTPError:
                return ticker, {}

        with ThreadPoolExecutor(self.max_workers) as pool:
            return dict(pool.map(fetch, unique))

    def get_orderbooks(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch the raw orderbook for each ticker, keyed by ticker."""
        unique = list(dict.fromkeys(tickers))

        def fetch(ticker: str) -> tuple[str, dict]:
            data = self._get(f"/markets/{ticker}/orderbook")
            return ticker, data.get("orderbook_fp") or data.get("orderbook") or {}

        fetched_at = datetime.now(timezone.utc)
        out: dict[str, dict] = {}
        with ThreadPoolExecutor(self.max_workers) as pool:
            for ticker, book in pool.map(fetch, unique):
                out[ticker] = {**book, "_fetched_at": fetched_at}
        return out
