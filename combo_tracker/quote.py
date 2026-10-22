"""Is a combo's price still live? Check one combo right now, and ask makers for a fresh quote.

A combo fill in the feed tells you someone got that price a few minutes ago, not that
you can get it now. Three things say whether you still can:

- the combo market's own orderbook: makers often rest orders on a combo after it
  trades, so its best ask is a price you can take right now;
- the newest fills on the same combo and side: a combo that keeps filling every few
  seconds at the same price has a maker still quoting it;
- a real RFQ (needs an API key): ask every maker for a quote at your size and see
  what comes back, like a sportsbook's "get odds" button.

Each price is compared with the legs' books now (mid and crossing the spread), so a
live price also shows whether it's still +EV.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from .engine import ComboBet, _parse_time, american_odds, legs_of, score_bet, taker_fee
from .orderbook import LegBook

# How the verdict reads the newest fills.
FRESH_SECONDS = 90  # a same-side fill at or under your price this recently = live
RECENT_SECONDS = 600  # fills older than this say nothing about now
PRICE_SLACK = 0.02  # a fill up to 2% (relative) over your price still counts as "your price"


@dataclass
class Fill:
    ts: datetime
    side: str
    price: float  # price of `side`, dollars
    contracts: float

    @property
    def stake(self) -> float:
        return self.price * self.contracts


@dataclass
class LiveCheck:
    ticker: str
    side: str
    price: float  # the price being checked (what the bettor paid, or what you'd pay)
    contracts: float
    checked_at: datetime
    title: str
    verdict: str  # LIVE, LIKELY, MOVED, STALE, NO DATA
    reason: str
    book_ask: float | None  # best price to buy `side` on the combo's own book
    book_ask_size: float
    book_at_price: float  # contracts offered at or under `price`
    book_levels: list[tuple[float, float]]
    fills: list[Fill]  # same combo, both sides, newest first
    best_now: float | None  # the lowest price that looks available now
    scored: dict  # score_bet() at `price` against the legs' books now
    ev_now: dict = field(default_factory=dict)  # EV % at best_now, mid and cross

    @property
    def side_fills(self) -> list[Fill]:
        return [f for f in self.fills if f.side == self.side]


def _fill(t: dict) -> Fill:
    side = t.get("taker_outcome_side") or t["taker_side"]
    price = float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])
    return Fill(_parse_time(t["created_time"]), side, price, float(t["count_fp"]))


def recent_trades(client, ticker: str, limit: int = 200) -> list[dict]:
    """The newest trades on one market, newest first."""
    if hasattr(client, "_get"):
        return client._get("/markets/trades", {"ticker": ticker, "limit": limit}).get("trades", [])
    trades = [t for t in getattr(client, "trades", []) if t["ticker"] == ticker]  # demo client
    return sorted(trades, key=lambda t: t["created_time"], reverse=True)[:limit]


def verdict(side: str, price: float, book_at_price: float, book_ask: float | None, fills: list[Fill],
            now: datetime) -> tuple[str, str, float | None]:
    """(verdict, reason, best price that looks available now)."""
    cap = price * (1 + PRICE_SLACK)
    mine = [f for f in fills if f.side == side and (now - f.ts).total_seconds() <= RECENT_SECONDS]
    fresh = [f for f in mine if (now - f.ts).total_seconds() <= FRESH_SECONDS]
    candidates = [p for p in [book_ask] + [f.price for f in fresh] if p is not None]
    best = min(candidates) if candidates else None

    def ago(f: Fill) -> str:
        s = max(0, int((now - f.ts).total_seconds()))
        return f"{s}s" if s < 120 else f"{s // 60}m"

    if book_at_price > 0:
        return "LIVE", f"{book_at_price:,.0f} contracts are resting on the combo's book at or under this price.", best
    fresh_ok = [f for f in fresh if f.price <= cap]
    if fresh_ok:
        f = fresh_ok[0]
        return "LIVE", (f"Filled {ago(f)} ago at {f.price * 100:.1f}¢ ({len(fresh_ok)} fill"
                        f"{'s' if len(fresh_ok) != 1 else ''} at this price in the last {FRESH_SECONDS}s)."), best
    ok = [f for f in mine if f.price <= cap]
    if ok and mine[0].price <= cap:
        f = ok[0]
        return "LIKELY", f"Last fill was {ago(f)} ago at {f.price * 100:.1f}¢. No resting order, so get a quote.", best
    if mine:
        f = mine[0]
        return "MOVED", f"Newest fill ({ago(f)} ago) was at {f.price * 100:.1f}¢, over {price * 100:.1f}¢.", best
    if book_ask is not None:
        return "MOVED", f"Nothing filled in {RECENT_SECONDS // 60} min; the combo's book asks {book_ask * 100:.1f}¢.", best
    if any(f.side == side for f in fills):
        return "STALE", f"No fills on this side in {RECENT_SECONDS // 60} min and no resting orders.", best
    return "NO DATA", "No recent fills and no resting orders on this combo.", best


def check(client, ticker: str, side: str, price: float, contracts: float, game_start=None,
          now: datetime | None = None, since: datetime | None = None) -> LiveCheck:
    """Check one combo + side at one price against its own book, its newest fills and its legs now.

    `since` is the time of the bet being checked: its own fills prove nothing about
    now, so only later fills count toward the verdict (all are still listed).
    """
    now = now or datetime.now(timezone.utc)
    market = client.get_markets([ticker]).get(ticker)
    if market is None:
        raise LookupError(f"Kalshi has no market {ticker}")
    legs = legs_of(market)
    leg_tickers = [l["market_ticker"] for l in legs]
    books = client.get_orderbooks([ticker] + leg_tickers)
    leg_markets = client.get_markets(leg_tickers, batch_size=100) if leg_tickers else {}
    events = client.get_events([l["event_ticker"] for l in legs]) if legs else {}

    combo = LegBook.from_raw(ticker, side, books.pop(ticker, {}) or {})
    at_price = sum(s for p, s in combo.asks if p <= price + 1e-9)
    fills = [_fill(t) for t in recent_trades(client, ticker)]
    fills.sort(key=lambda f: f.ts, reverse=True)
    later = [f for f in fills if since is None or f.ts > since]
    word, reason, best = verdict(side, price, at_price, combo.best_ask, later, now)
    if since is not None and word in ("STALE", "NO DATA") and not any(f.side == side for f in later):
        word, reason = "STALE", "Nobody has filled this side since this bet, and no orders are resting at this price."

    bet = ComboBet(ticker, side, contracts, price, now, now, 1)
    scored = score_bet(bet, market, leg_markets, books, now=now, events=events, game_start=game_start)
    ev_now = {}
    if best:
        ev_now = {"mid": scored["fair_mid"] / best - 1 if scored["fair_mid"] else None,
                  "cross": scored["fair_crx"] / best - 1 if scored["fair_crx"] else None}
    return LiveCheck(ticker, side, price, contracts, now, scored["title"], word, reason, combo.best_ask,
                     combo.top_ask_size, at_price, combo.asks[:6], fills, best, scored, ev_now)


# ---------- RFQs: ask makers for a quote ----------
class RfqError(RuntimeError):
    pass


@dataclass
class Quote:
    id: str
    maker: str  # the maker's communications ID (a hash), shortened for display
    side: str  # the side you'd buy
    price: float  # per contract, before fees
    contracts: float
    status: str
    created: datetime | None

    @property
    def fee(self) -> float:
        return taker_fee(self.price, self.contracts) if self.contracts else 0.0

    @property
    def all_in(self) -> float:
        """Price per contract including your taker fee."""
        return self.price + self.fee / self.contracts if self.contracts else self.price


def quote_for(raw: dict, side: str) -> Quote | None:
    """A maker's quote from the requester's point of view.

    The maker bids for YES (yes_bid) and for NO (no_bid). Buying YES means the maker
    takes NO at their no_bid, so your YES price is 1 - no_bid (and vice versa).
    """
    other = float(raw.get("no_bid_dollars" if side == "yes" else "yes_bid_dollars") or 0)
    if not 0 < other < 1:
        return None
    return Quote(id=raw["id"], maker=(raw.get("creator_id") or "")[:4], side=side, price=round(1 - other, 6),
                 contracts=float(raw.get("contracts_fp") or 0), status=raw.get("status", ""),
                 created=_parse_time(raw.get("created_ts")))


class RfqDesk:
    """Creates, reads and deletes your own RFQs. Never accepts a quote."""

    def __init__(self, client, auth):
        self.client = client
        self.auth = auth

    def _send(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = f"{self.client.base_url}{path}"
        self.client.limiter.wait()
        resp = self.client.session.request(method, url, params=params, json=body, timeout=self.client.timeout,
                                           headers=self.auth.headers(method, urlparse(url).path))
        if not resp.ok:
            raise RfqError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def create(self, ticker: str, dollars: float) -> str:
        """Ask for quotes on `ticker` for about `dollars` all-in. Replaces your other open RFQs."""
        body = {"market_ticker": ticker, "target_cost_dollars": f"{dollars:.2f}", "rest_remainder": False,
                "replace_existing": True}
        rid = self._send("POST", "/communications/rfqs", body=body).get("id")
        if not rid:
            raise RfqError("Kalshi didn't return an RFQ id")
        return rid

    def quotes(self, rfq_id: str) -> list[dict]:
        return self._send("GET", "/communications/quotes", {"rfq_id": rfq_id, "limit": 100}).get("quotes") or []

    def rfq(self, rfq_id: str) -> dict:
        return self._send("GET", f"/communications/rfqs/{rfq_id}").get("rfq") or {}

    def cancel(self, rfq_id: str) -> None:
        self._send("DELETE", f"/communications/rfqs/{rfq_id}")


class DemoRfqDesk:
    """Offline stand-in: makers answer within a few seconds, a bit above the legs' mid."""

    def __init__(self, fair: dict[str, float]):
        self.fair = fair  # ticker -> fair YES mid
        self.open: dict[str, tuple[str, float, float]] = {}

    def create(self, ticker: str, dollars: float) -> str:
        rid = f"demo-rfq-{int(time.time() * 1000)}"
        self.open = {rid: (ticker, dollars, time.time())}
        return rid

    def quotes(self, rfq_id: str) -> list[dict]:
        if rfq_id not in self.open:
            return []
        ticker, dollars, t0 = self.open[rfq_id]
        fair = self.fair.get(ticker, 0.3)
        out = []
        for i, (lag, vig, maker) in enumerate([(1.5, 0.10, "1a8d4f"), (3, 0.13, "ede2b0"), (5, 0.16, "ab9d77")]):
            if time.time() - t0 < lag:
                continue
            yes = min(0.98, fair * (1 + vig))
            no = min(0.98, (1 - fair) * (1 + vig / 3))
            out.append({"id": f"{rfq_id}-q{i}", "creator_id": maker, "status": "open",
                        "contracts_fp": f"{dollars / yes:.2f}", "yes_bid_dollars": f"{1 - no:.4f}",
                        "no_bid_dollars": f"{1 - yes:.4f}",
                        "created_ts": datetime.now(timezone.utc).isoformat()})
        return out

    def cancel(self, rfq_id: str) -> None:
        self.open.pop(rfq_id, None)


def odds(p: float | None) -> str:
    o = american_odds(p)
    return "–" if o is None else (f"+{o}" if o > 0 else str(o))
