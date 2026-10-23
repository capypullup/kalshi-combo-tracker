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

import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

from .engine import ComboBet, _game_key, _parse_time, american_odds, kelly_fraction, legs_of, score_bet, taker_fee
from .orderbook import LegBook

# How the verdict reads the newest fills.
FRESH_SECONDS = 90  # a same-side fill at or under your price this recently = live
RECENT_SECONDS = 600  # fills older than this say nothing about now
PRICE_SLACK = 0.02  # a fill up to 2% (relative) over your price still counts as "your price"
LEG_TTL = 30  # seconds the legs' books are reused between checks of the same combo (WATCH)


def cents(p: float | None) -> str:
    """'40.9¢', or '0.17¢' for long shots where a tenth of a cent is a big move."""
    if p is None:
        return "–"
    return f"{p * 100:.2f}¢" if p < 0.1 else f"{p * 100:.1f}¢"


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
        return "LIVE", (f"Filled {ago(f)} ago at {cents(f.price)} ({len(fresh_ok)} fill"
                        f"{'s' if len(fresh_ok) != 1 else ''} at this price in the last {FRESH_SECONDS}s)."), best
    ok = [f for f in mine if f.price <= cap]
    if ok and mine[0].price <= cap:
        f = ok[0]
        return "LIKELY", f"Last fill was {ago(f)} ago at {cents(f.price)}. No resting order, so get a quote.", best
    if mine:
        f = mine[0]
        return "MOVED", f"Newest fill ({ago(f)} ago) was at {cents(f.price)}, over {cents(price)}.", best
    if book_ask is not None:
        return "MOVED", f"Nothing filled in {RECENT_SECONDS // 60} min; the combo's book asks {cents(book_ask)}.", best
    if any(f.side == side for f in fills):
        return "STALE", f"No fills on this side in {RECENT_SECONDS // 60} min and no resting orders.", best
    return "NO DATA", "No recent fills and no resting orders on this combo.", best


def check(client, ticker: str, side: str, price: float, contracts: float, game_start=None,
          now: datetime | None = None, since: datetime | None = None, cache: dict | None = None) -> LiveCheck:
    """Check one combo + side at one price against its own book, its newest fills and its legs now.

    `since` is the time of the bet being checked: its own fills prove nothing about
    now, so only later fills count toward the verdict (all are still listed).
    `cache` (any dict kept between calls) holds the legs' data for LEG_TTL seconds, so
    re-checking the same combo only fetches its own book and fills.
    """
    now = now or datetime.now(timezone.utc)
    hit = (cache or {}).get(ticker)
    if hit and time.time() - hit[0] < LEG_TTL:
        market, leg_books, leg_markets, events = hit[1]
    else:
        market = client.get_markets([ticker]).get(ticker)
        if market is None:
            raise LookupError(f"Kalshi has no market {ticker}")
        legs = legs_of(market)
        leg_tickers = [l["market_ticker"] for l in legs]
        leg_books = client.get_orderbooks(leg_tickers) if leg_tickers else {}
        leg_markets = client.get_markets(leg_tickers, batch_size=100) if leg_tickers else {}
        events = client.get_events([l["event_ticker"] for l in legs]) if legs else {}
        if cache is not None:
            cache.clear()  # one combo at a time
            cache[ticker] = (time.time(), (market, leg_books, leg_markets, events))
    books = {**leg_books, **client.get_orderbooks([ticker])}

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


# ---------- RFQs: ask makers for a quote, and take one ----------
class RfqError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code  # Kalshi's error code or details, e.g. INCORRECT_API_KEY_SIGNATURE


def explain(exc: Exception) -> str:
    """What a Kalshi error means for you, in a sentence."""
    code, status = getattr(exc, "code", None) or "", getattr(exc, "status", None)
    if code == "INCORRECT_API_KEY_SIGNATURE":
        return ("Kalshi says the signature doesn't match the key ID. The KALSHI_KEY_ID and KALSHI_PRIVATE_KEY in the "
                "app's secrets are probably from two different keys: paste both again from the same key.")
    if status == 401:
        return f"Kalshi didn't accept the API key ({code or 'HTTP 401'}). Check KALSHI_KEY_ID in the app's secrets."
    if status == 403 and any(w in code.lower() for w in ("scope", "permission", "write")):
        return "This API key can't trade. Make a key with read and write access on Kalshi."
    if "INSUFFICIENT_BALANCE" in code.upper() or "insufficient" in str(exc).lower():
        return "Not enough cash in your Kalshi account for this size."
    if status == 409:
        return "You already have an open request on this combo (maybe from the Kalshi app). Try again in a few seconds."
    if "RFQ_CLOSED" in code.upper():
        return "That request closed (it expired or filled). Get a new quote."
    return str(exc)[:300]


@dataclass
class Quote:
    id: str
    maker: str  # the maker's communications ID (a hash), shortened for display
    side: str  # the side you'd buy
    price: float  # per contract, before fees
    contracts: float
    status: str
    created: datetime | None
    rfq_id: str = ""
    two_sided: bool = False  # the maker also bids your side (so a wrongly read accept could fill the other side)

    @property
    def fee(self) -> float:
        return taker_fee(self.price, self.contracts) if self.contracts else 0.0

    @property
    def all_in(self) -> float:
        """Price per contract including your taker fee."""
        return self.price + self.fee / self.contracts if self.contracts else self.price

    @property
    def cost(self) -> float:
        return self.price * self.contracts + self.fee


def quote_for(raw: dict, side: str) -> Quote | None:
    """A maker's quote from the requester's point of view.

    The maker bids for YES (yes_bid) and for NO (no_bid). Buying YES means the maker
    takes NO at their no_bid, so your YES price is 1 - no_bid (and vice versa). Kalshi's
    FIX docs say the same: a BUY accepts the maker's NO quote.
    """
    taken = "no" if side == "yes" else "yes"  # the maker's bid you hit
    other = float(raw.get(f"{taken}_bid_dollars") or 0)
    if not 0 < other < 1:
        return None
    # With a dollar target, Kalshi sizes the quote per maker bid: buying YES fills the maker's NO
    # bid, so its size is no_contracts_fp (checked against a live quote: $10 at 33.7¢ + fee = 28.35).
    n = raw.get(f"{taken}_contracts_fp") or raw.get("contracts_fp") or 0
    return Quote(id=raw["id"], maker=(raw.get("creator_id") or "")[:4], side=side, price=round(1 - other, 6),
                 contracts=float(n), status=raw.get("status", ""), created=_parse_time(raw.get("created_ts")),
                 rfq_id=raw.get("rfq_id", ""), two_sided=float(raw.get(f"{side}_bid_dollars") or 0) > 0)


USER_IDS: dict[str, str] = {}  # API key ID -> your private user ID (from your own RFQ), to list your quotes


class RfqDesk:
    """Asks makers for quotes with your API key, and takes one when you confirm.

    RFQs hide your communications ID from makers (obscure_creator_id). Kalshi allows one open
    RFQ per combo, so one you already have open on the same combo (an earlier tap, or that
    combo's slip in the Kalshi app) is closed first. Others are left alone.
    """

    def __init__(self, client, auth):
        self.client = client
        self.auth = auth

    def _send(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = f"{self.client.base_url}{path}"
        self.client.limiter.wait()
        try:
            resp = self.client.session.request(method, url, params=params, json=body, timeout=self.client.timeout,
                                               headers=self.auth.headers(method, urlparse(url).path))
        except requests.RequestException as exc:
            # No URL in the message: a quotes read carries your user ID in its query string.
            raise RfqError(f"{method} {path}: no answer from Kalshi ({type(exc).__name__})", None, "NETWORK") from None
        if not resp.ok:
            try:
                err = resp.json()
                err = err.get("error", err) if isinstance(err, dict) else {}
            except ValueError:
                err = {}
            code = err.get("details") or err.get("code") if isinstance(err, dict) else None
            raise RfqError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}", resp.status_code,
                           code if isinstance(code, str) else None)
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            raise RfqError(f"{method} {path}: Kalshi's answer wasn't JSON", resp.status_code, "NETWORK") from None

    def create(self, ticker: str, dollars: float) -> str:
        """Ask for quotes on `ticker` for about `dollars`, fees included."""
        body = {"market_ticker": ticker, "target_cost_dollars": f"{dollars:.2f}", "rest_remainder": False,
                "obscure_creator_id": True}
        try:
            rid = self._send("POST", "/communications/rfqs", body=body).get("id")
        except RfqError as exc:
            if exc.status != 409:
                raise
            # One open RFQ per combo: close ours (an earlier tap) and ask again.
            mine = self._send("GET", "/communications/rfqs", {"market_ticker": ticker, "status": "open",
                                                              "user_filter": "self", "limit": 20}).get("rfqs") or []
            for r in mine:
                self.cancel(r["id"])
            rid = self._send("POST", "/communications/rfqs", body=body).get("id")
        if not rid:
            raise RfqError("Kalshi didn't return an RFQ id")
        return rid

    def quotes(self, rfq_id: str) -> list[dict]:
        # Kalshi only lists quotes filtered by a user ID (403 otherwise): yours, as the RFQ's creator.
        uid = self._user_id(rfq_id)
        return self._send("GET", "/communications/quotes", {"rfq_id": rfq_id, "rfq_creator_user_id": uid,
                                                            "limit": 100}).get("quotes") or []

    def _user_id(self, rfq_id: str) -> str:
        uid = USER_IDS.get(self.auth.key_id)
        if not uid:
            uid = self.rfq(rfq_id).get("creator_user_id")
            if not uid:
                raise RfqError("Kalshi didn't say who created the RFQ")
            USER_IDS[self.auth.key_id] = uid
        return uid

    def quote(self, rfq_id: str, quote_id: str) -> dict:
        return self._send("GET", f"/communications/rfqs/{rfq_id}/quotes/{quote_id}").get("quote") or {}

    def rfq(self, rfq_id: str) -> dict:
        return self._send("GET", f"/communications/rfqs/{rfq_id}").get("rfq") or {}

    def cancel(self, rfq_id: str) -> None:
        self._send("DELETE", f"/communications/rfqs/{rfq_id}")

    def accept(self, rfq_id: str, quote_id: str, side: str) -> None:
        """Take a quote: buy `side` at the maker's price. The maker then has 3 s to confirm.

        Kalshi's REST docs only say "the side of the quote to accept". Its FIX docs map a BUY (which
        takes the maker's NO bid) to AcceptedSide = Yes, so accepted_side is the side you buy.
        """
        self._send("PUT", f"/communications/rfqs/{rfq_id}/quotes/{quote_id}/accept", body={"accepted_side": side})

    def fills(self, ticker: str) -> list[dict]:
        """Your newest fills on one market."""
        return self._send("GET", "/portfolio/fills", {"ticker": ticker, "limit": 20}).get("fills") or []

    def create_combo(self, collection: str, legs: list[dict]) -> str:
        """The combo market for these legs (made the first time anyone asks for it)."""
        selected = [{"market_ticker": l["market_ticker"], "event_ticker": l["event_ticker"], "side": l["side"]}
                    for l in legs]
        out = self._send("POST", f"/multivariate_event_collections/{collection}", body={"selected_markets": selected})
        if not out.get("market_ticker"):
            raise RfqError("Kalshi didn't return a combo ticker")
        return out["market_ticker"]

    def key_check(self) -> dict:
        """Can this key read, and can it trade? Uses calls that change nothing: listing your API
        keys, and an RFQ on a market that doesn't exist (404 = signed fine)."""
        out = {"kind": self.auth.kind, "fingerprint": self.auth.fingerprint(), "key_id_end": self.auth.key_id[-4:],
               "scopes": None}
        try:
            keys = self._send("GET", "/api_keys").get("api_keys") or []
            out["read"] = "ok"
            mine = [k for k in keys if k.get("api_key_id") == self.auth.key_id]
            out["scopes"] = mine[0].get("scopes") if mine else None
        except RfqError as exc:
            out["read"] = exc.code or f"HTTP {exc.status}"
        try:
            self._send("POST", "/communications/rfqs", body={"market_ticker": "CAPYEV-KEYCHECK", "contracts": 1,
                                                             "rest_remainder": False})
            out["write"] = "ok"
        except RfqError as exc:
            out["write"] = "ok" if exc.status in (400, 404) else exc.code or f"HTTP {exc.status}"
        return out


class DemoRfqDesk:
    """Offline stand-in: makers answer within a few seconds, a bit above the legs' mid."""

    def __init__(self, fair: dict[str, float]):
        self.fair = fair  # ticker -> fair YES mid
        self.open: dict[str, tuple[str, float, float]] = {}
        self.accepted: dict[str, tuple[str, float, float]] = {}  # ticker -> (side, price, contracts)

    def create(self, ticker: str, dollars: float) -> str:
        rid = f"demo-rfq-{int(time.time() * 1000)}-{len(self.open)}"
        self.open[rid] = (ticker, dollars, time.time())
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
            fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731  (per contract; Kalshi sizes the target cost with it)
            out.append({"id": f"{rfq_id}-q{i}", "rfq_id": rfq_id, "creator_id": maker, "status": "open",
                        "contracts_fp": "0.00", "no_contracts_fp": f"{dollars / (yes + fee(yes)):.2f}",
                        "yes_contracts_fp": f"{dollars / (no + fee(no)):.2f}", "yes_bid_dollars": f"{1 - no:.4f}",
                        "no_bid_dollars": f"{1 - yes:.4f}", "created_ts": datetime.now(timezone.utc).isoformat()})
        return out

    def quote(self, rfq_id: str, quote_id: str) -> dict:
        q = next((q for q in self.quotes(rfq_id) if q["id"] == quote_id), {})
        return {**q, "status": "executed"} if q and rfq_id in self.accepted else q

    def cancel(self, rfq_id: str) -> None:
        self.open.pop(rfq_id, None)

    def accept(self, rfq_id: str, quote_id: str, side: str) -> None:
        raw = next((q for q in self.quotes(rfq_id) if q["id"] == quote_id), None)
        if raw is None:
            raise RfqError("quote closed", 400, "RFQ_CLOSED")
        q = quote_for(raw, side)
        self.accepted[rfq_id] = (side, q.price, q.contracts)

    def fills(self, ticker: str) -> list[dict]:
        out = []
        for rid, (side, price, n) in self.accepted.items():
            if self.open.get(rid, ("",))[0] == ticker:
                out.append({"ticker": ticker, "outcome_side": side, "count_fp": f"{n:.2f}",
                            "yes_price_dollars": f"{price if side == 'yes' else 1 - price:.4f}",
                            "no_price_dollars": f"{1 - price if side == 'yes' else price:.4f}",
                            "created_time": datetime.now(timezone.utc).isoformat()})
        return out

    def create_combo(self, collection: str, legs: list[dict]) -> str:
        return "KXMVEDEMO-" + "-".join(sorted(l["market_ticker"][-6:] + l["side"][0] for l in legs))

    def key_check(self) -> dict:
        return {"kind": "demo", "fingerprint": "demo", "key_id_end": "demo", "scopes": ["read", "write"],
                "read": "ok", "write": "ok"}


def odds(p: float | None) -> str:
    o = american_odds(p)
    return "–" if o is None else (f"+{o}" if o > 0 else str(o))


def mult(p: float | None) -> str:
    """Price -> what $1 returns, like the Kalshi app: 0.12 -> '8.33x'."""
    if not p or p <= 0:
        return "–"
    x = 1 / p
    return f"{x:,.0f}x" if x >= 100 else f"{x:.1f}x" if x >= 10 else f"{x:.2f}x"


# ---------- sizing ----------
UNIT_DOLLARS = 1000.0  # Kenny's unit (2026-10-23)
KELLY = 0.25  # the Combos page's default: quarter Kelly
BANKROLL_UNITS = 100  # 1u = 1% of bankroll, also the Combos page's default


def kelly_stake(fair: float | None, price: float, unit: float = UNIT_DOLLARS, kelly: float = KELLY,
                bankroll_units: float = BANKROLL_UNITS) -> float:
    """Dollars to bet at `price` (fee included) if the true odds are `fair`."""
    f = kelly_fraction(fair, price) or 0.0
    return f * kelly * bankroll_units * unit


# ---------- then vs now ----------
@dataclass
class LegMove:
    name: str
    side: str
    then: float | None
    now: float | None

    @property
    def delta(self) -> float | None:
        return None if self.then is None or self.now is None else self.now - self.then

    @property
    def moved(self) -> bool:
        return self.delta is not None and abs(self.delta) >= 0.015 - 1e-9  # a point and a half


def leg_moves(then_row: dict, now_scored: dict) -> list[LegMove]:
    """Each leg's mid (for the side in the combo) when CapyEV scored the bet, and now."""
    before = {l["ticker"]: l for l in then_row.get("leg_detail") or []}
    return [LegMove(l["leg"], l["side"], (before.get(l["ticker"]) or {}).get("mid"), l["mid"])
            for l in now_scored.get("leg_detail") or []]


# ---------- extra ("dummy") legs ----------
# Series prefixes, checked in order, for the sport chips.
SPORTS = [
    ("NFL", ("KXNFL",)), ("NBA", ("KXNBA",)), ("NHL", ("KXNHL",)), ("MLB", ("KXMLB",)),
    ("CFB", ("KXNCAAF",)), ("CBB", ("KXNCAAMB", "KXNCAAB", "KXNCAAWB", "KXMARMAD")), ("WNBA", ("KXWNBA",)),
    ("Tennis", ("KXATP", "KXWTA", "KXITF", "KXCHALL", "KXTENNIS", "KXDAVIS", "KXUSO", "KXWIMB", "KXFO", "KXAO")),
    ("Soccer", ("KXEPL", "KXUCL", "KXUEL", "KXLALIGA", "KXSERIEA", "KXBUNDES", "KXLIGUE", "KXMLS", "KXSOCCER",
                "KXFIFA", "KXWC", "KXEFL", "KXLIGAMX", "KXLIGAEXP", "KXEREDIV", "KXUECL", "KXSAUDI", "KXBRASIL", "KXARG")),
    ("UFC", ("KXUFC", "KXMMA")), ("Golf", ("KXPGA", "KXGOLF", "KXLIV")),
    ("Esports", ("KXLOL", "KXCS2", "KXCSGO", "KXVALORANT", "KXDOTA", "KXR6", "KXCOD")),
]


def sport_of(event_ticker: str) -> str:
    series = event_ticker.split("-", 1)[0].upper()
    for name, prefixes in SPORTS:
        if series.startswith(prefixes):
            return name
    return "Other"


@dataclass
class ExtraLeg:
    market_ticker: str
    event_ticker: str
    side: str
    name: str
    event: str
    sport: str
    prob: float  # mid of `side`
    bid: float  # what crossing the spread values it at
    spread: float
    live: bool
    start: datetime | None

    def as_leg(self) -> dict:
        return {"market_ticker": self.market_ticker, "event_ticker": self.event_ticker, "side": self.side}


def extra_legs(markets: dict[str, dict], events: dict[str, dict], combo_legs: list[dict], allowed: set | None = None,
               game_start=None, now: datetime | None = None, lo: float = 0.80, hi: float = 0.985,
               max_spread: float = 0.03, min_size: float = 50, max_hours: float = 36) -> list[ExtraLeg]:
    """Near-certain legs (80-98.5% at the mid, tight and deep books) on other games, to add to a
    combo and see whether makers quote the bigger combo better than the extra legs cost.

    `markets` and `events` are the leg markets and events CapyEV already has from recent combos,
    so every candidate is a leg other bettors combine. Legs on the combo's own games are left
    out: they're correlated with it, so their mid would misprice the bigger combo.
    """
    now = now or datetime.now(timezone.utc)
    games = {_game_key(l["event_ticker"]) for l in combo_legs}
    out = []
    for t, m in markets.items():
        ev = m.get("event_ticker") or ""
        if m.get("status", "active") != "active" or not ev or _game_key(ev) in games:
            continue
        if allowed is not None and ev not in allowed:
            continue
        try:
            bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
            sizes = float(m.get("yes_bid_size_fp") or 0), float(m.get("yes_ask_size_fp") or 0)
        except (TypeError, ValueError):
            continue
        if not 0 < bid < ask < 1 or ask - bid > max_spread + 1e-9 or min(sizes) < min_size:
            continue
        expiry = _parse_time(m.get("expected_expiration_time"))
        if expiry is not None and (expiry - now).total_seconds() > max_hours * 3600:
            continue  # money tied up for days
        mid = (bid + ask) / 2
        side, prob, worst = ("yes", mid, bid) if mid >= 0.5 else ("no", 1 - mid, 1 - ask)
        if not lo <= prob <= hi:
            continue
        start = game_start(ev) if game_start else None
        live = start is not None and start <= now
        title = (events.get(ev) or {}).get("title") or m.get("title") or ev
        out.append(ExtraLeg(t, ev, side, m.get("yes_sub_title") or m.get("title") or t, title, sport_of(ev), prob,
                            worst, ask - bid, live, start))
    out.sort(key=lambda x: (x.live, -x.prob, x.spread))  # makers often skip combos with a leg in play
    seen: set[str] = set()  # one leg per game: two on the same game are correlated
    return [x for x in out if not (_game_key(x.event_ticker) in seen or seen.add(_game_key(x.event_ticker)))]


# ---------- pasting ----------
TICKER_RE = re.compile(r"KXMVE[A-Z0-9]*-[A-Z0-9]+(?:-[A-Z0-9]+)*", re.I)


def parse_paste(text: str) -> dict | None:
    """A combo from whatever was pasted: a ticker, a CapyEV /quote link, or a Kalshi link."""
    text = (text or "").strip()
    if not text:
        return None
    out = {"ticker": None, "side": None, "price": None, "contracts": None}
    if re.search(r"[?&]t=", text):  # a link, with or without https://
        q = parse_qs(urlparse(text if "://" in text else "https://x/" + text.lstrip("/")).query)
        t = (q.get("t") or [""])[0].strip()
        if TICKER_RE.fullmatch(t):  # another site's t= is no ticker: look for one in the text instead
            out["ticker"] = t.upper()
            out["side"] = (q.get("side") or [""])[0].strip().lower() or None
            for key, name in (("p", "price"), ("n", "contracts")):
                try:
                    v = float(q[key][0]) if q.get(key) else None
                except ValueError:
                    v = None
                ok = v is not None and math.isfinite(v) and v > 0 and (name != "price" or v < 1)
                out[name] = v if ok else None
    if not out["ticker"]:
        m = TICKER_RE.search(text)
        if not m:
            return None
        out["ticker"] = m.group(0).upper()
    if out["side"] not in ("yes", "no"):
        out["side"] = None
    return out


class RfqReaper:
    """Cancels RFQs nobody is watching anymore (you closed the page, say) after max_age seconds."""

    def __init__(self, max_age: float = 50):
        self.max_age = max_age
        self.open: dict[str, tuple[float, object]] = {}  # rfq id -> (created, desk)
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def add(self, desk, rfq_id: str) -> None:
        with self.lock:
            self.open[rfq_id] = (time.time(), desk)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="rfq-reaper", daemon=True)
                self.thread.start()

    def sweep(self) -> None:
        with self.lock:
            due = [(rid, desk) for rid, (t, desk) in self.open.items() if time.time() - t >= self.max_age]
            for rid, _ in due:
                del self.open[rid]
        for rid, desk in due:
            try:
                desk.cancel(rid)
            except Exception:  # already cancelled, filled or expired
                pass

    def _run(self) -> None:
        while True:
            time.sleep(5)
            self.sweep()
            with self.lock:
                if not self.open:
                    self.thread = None
                    return


REAPER = RfqReaper()
