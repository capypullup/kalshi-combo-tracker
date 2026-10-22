"""Thin wrapper around Kalshi's public (no-key) REST API."""

from __future__ import annotations

import base64
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urlparse

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
# Kalshi's social API (the one kalshi.com profiles use). Also public and keyless.
SOCIAL_URL = "https://api.elections.kalshi.com/v1/social"
COMBO_PREFIX = "KXMVE"
MAX_CANDLES = 10_000  # per /markets/candlesticks request, across all its markets


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


class KalshiAuth:
    """Signs requests with a Kalshi API key: RSA-PSS over timestamp + method + path.

    Only the RFQ endpoints need it. The app only ever makes GET requests with it.
    """

    def __init__(self, key_id: str, private_key_pem: str):
        from cryptography.hazmat.primitives import serialization

        self.key_id = key_id
        self.key = serialization.load_pem_private_key(private_key_pem.strip().encode(), password=None)

    @classmethod
    def from_env(cls, secrets: dict | None = None) -> "KalshiAuth | None":
        """KALSHI_KEY_ID + KALSHI_PRIVATE_KEY from Streamlit secrets or the environment, if set."""
        get = lambda k: (secrets or {}).get(k) or os.environ.get(k)  # noqa: E731
        key_id, pem = get("KALSHI_KEY_ID"), get("KALSHI_PRIVATE_KEY")
        return cls(key_id, pem.replace("\\n", "\n")) if key_id and pem else None

    def headers(self, method: str, path: str) -> dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        sig = self.key.sign(f"{ts}{method}{path}".encode(),
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 15.0, max_workers: int = 4,
                 rate_per_sec: float = 10.0, auth: KalshiAuth | None = None):
        self.auth = auth
        self.base_url = base_url
        self.timeout = timeout
        self.max_workers = max_workers
        self.limiter = RateLimiter(rate_per_sec)
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        self.session.headers["User-Agent"] = "kalshi-combo-tracker (+https://github.com/capypullup/kalshi-combo-tracker)"
        self.batch_orderbooks_ok: bool | None = None  # learned on first try
        self.requests_made = 0

    def _get(self, path: str, params: dict | None = None, retries: int = 5, signed: bool = False) -> dict:
        return self._get_url(f"{self.base_url}{path}", params, retries, signed)

    def _get_url(self, url: str, params: dict | None = None, retries: int = 5, signed: bool = False) -> dict:
        if signed and self.auth is None:
            raise RuntimeError("this call needs a Kalshi API key (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY)")
        for attempt in range(retries + 1):
            self.limiter.wait()
            self.requests_made += 1
            headers = self.auth.headers("GET", urlparse(url).path) if signed else None
            resp = self.session.get(url, params=params, timeout=self.timeout, headers=headers)
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

    def get_milestones(self, min_start: datetime, until: datetime, category: str = "Sports",
                       max_pages: int = 20) -> list[dict]:
        """Scheduled events (games, matches) starting between min_start and until.

        Each milestone has a `start_date` and the event tickers it covers, props
        included. Kalshi returns them oldest start first, so paging stops at `until`.
        """
        params = {"limit": 500, "category": category, "minimum_start_date": min_start.strftime("%Y-%m-%dT%H:%M:%SZ")}
        out: list[dict] = []
        for _ in range(max_pages):
            data = self._get("/milestones", params)
            page = data.get("milestones") or []
            out.extend(page)
            cursor = data.get("cursor")
            if not page or not cursor or page[-1].get("start_date", "") > until.strftime("%Y-%m-%dT%H:%M:%SZ"):
                break
            params["cursor"] = cursor
        return out

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

    # ---- price history ----
    def get_candles(self, tickers: list[str], start_ts: int, end_ts: int) -> dict[str, list[dict]]:
        """One-minute candles (yes bid/ask at each minute's close) for many markets over one span.

        Kalshi caps a request at MAX_CANDLES candles across all its markets, so the
        tickers are split into as many requests as that takes.
        """
        minutes = max(1, (end_ts - start_ts) // 60 + 1)
        per_call = max(1, min(100, MAX_CANDLES // minutes))
        unique = list(dict.fromkeys(tickers))
        out: dict[str, list[dict]] = {}
        for i in range(0, len(unique), per_call):
            data = self._get("/markets/candlesticks", {"market_tickers": ",".join(unique[i : i + per_call]),
                                                       "start_ts": start_ts, "end_ts": end_ts, "period_interval": 1})
            for m in data.get("markets", []):
                out[m["market_ticker"]] = m.get("candlesticks") or []
        return out

    # ---- social (public profiles) ----
    def social_trades(self, nickname: str | None = None, series_ticker: str | None = None,
                      cursor: str | None = None, limit: int = 100, market_ticker: str | None = None) -> dict:
        """One page (max 100, newest first) of trades with the maker's and taker's public
        nicknames. A nickname is blank unless that trader's profile shows their trades.
        For a profile with hidden trades, the response has visibility_state "hidden"."""
        params = {"limit": limit}
        if nickname:
            params["nickname"] = nickname
        if series_ticker:
            params["series_ticker"] = series_ticker
        if market_ticker:
            params["market_ticker"] = market_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get_url(f"{SOCIAL_URL}/trades", params)

    def profile_metrics(self, nickname: str, days: int | None = None) -> dict:
        """A profile's PnL (in 1/100 cents), markets traded, etc. `days` limits it to recent days (30 works)."""
        params = {"nickname": nickname}
        if days:
            params["since_day_before"] = days
        return self._get_url(f"{SOCIAL_URL}/profile/metrics", params).get("metrics") or {}

    # ---- RFQs (need an API key) ----
    def get_rfqs(self, cursor: str | None = None, status: str | None = None, limit: int = 100) -> dict:
        """One page of everyone's RFQs. creator_id is a pseudonymous hash, stable per user."""
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        return self._get("/communications/rfqs", params, signed=True)

    def get_rfq(self, rfq_id: str) -> dict:
        return self._get(f"/communications/rfqs/{rfq_id}", signed=True).get("rfq") or {}
