"""Track public Kalshi profiles: their PnL, how much they trade, and the EV of their
trades against the orderbook at the moment they traded.

Kalshi's main trade feed has no trader ID, but its social API (the one kalshi.com
profiles use, public and keyless) returns the same trades with the maker's and
taker's nicknames filled in for anyone whose profile shows their trades. So:

1. Discovery: page the social trade feed for tennis series and note every nickname.
2. History: for the busiest of those nicknames (and any you add), page their own
   trade history.
3. EV: price each fill against the market's bid/ask in the minute it happened (from
   one-minute candles). A combo is priced from its legs' books, as on the combo page.
4. PnL: settled fills pay out on the market's result.

Comparing realized PnL with that EV separates bettors who beat the book (PnL well
above EV, many trades) from ones who just got lucky.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from . import activity
from .client import COMBO_PREFIX
from .engine import taker_fee

THREAD_NAME = "bettor-tracker"
# Match-winner, set-winner and doubles series for the tours Kalshi lists. New series
# are still caught in bettors' histories by TENNIS_RE.
TENNIS_SERIES = [
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH",
    "KXATPSETWINNER", "KXWTASETWINNER", "KXATPDOUBLES", "KXWTADOUBLES", "KXITFDOUBLES", "KXITFWDOUBLES",
]
TENNIS_RE = re.compile(r"^KX(ATP|WTA|ITF)")
MONEY_UNIT = 10_000  # the social API's money fields are in 1/100 of a cent
# Row summaries: every trade or tennis only, over the whole history or the last 30 days.
SCOPES = ("all", "tennis", "all30", "tennis30")
DAY = 86400


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True, slots=True)
class Fill:
    """One trade seen from the bettor's side: they bought `side` at `price`."""

    trade_id: str
    ts: float
    ticker: str
    side: str  # "yes" or "no". Selling YES is the same position as buying NO.
    price: float  # dollars per contract of `side`
    count: float
    taker: bool

    @property
    def stake(self) -> float:
        return self.price * self.count

    @property
    def fee(self) -> float:
        return taker_fee(self.price, self.count) if self.taker else 0.0


def to_fill(trade: dict, nickname: str) -> Fill | None:
    """A social-feed trade from `nickname`'s side, or None if they weren't in it.

    taker_side is the outcome the taker bought (checked against the v2 feed's
    taker_outcome_side); price_dollars is the YES price. The maker holds the other side.
    """
    if trade.get("taker_nickname") == nickname:
        side, taker = trade["taker_side"], True
    elif trade.get("maker_nickname") == nickname:
        side, taker = ("no" if trade["taker_side"] == "yes" else "yes"), False
    else:
        return None
    yes = float(trade["price_dollars"])
    return Fill(trade["trade_id"], _ts(trade["create_date"]), trade["ticker"], side,
                yes if side == "yes" else 1 - yes, float(trade["count_fp"]), taker)


def is_tennis(ticker: str, legs: list[dict] | None = None) -> bool:
    if TENNIS_RE.match(ticker):
        return True
    return any(TENNIS_RE.match(l.get("event_ticker") or l.get("market_ticker", "")) for l in legs or [])


def _price(candle: dict, book: str, which: str) -> float | None:
    v = (candle.get(book) or {}).get(f"{which}_dollars")
    return None if v is None else float(v)


def mid_at(candles: list[dict], ts: float) -> float | None:
    """The YES mid a trader saw just before trading at `ts`.

    Uses the open of the one-minute candle the trade falls in (the book at the start
    of that minute), else the close of the last candle before it. None when the book
    wasn't two-sided.
    """
    bid = ask = None
    now = next((c for c in candles if c["end_period_ts"] - 60 < ts <= c["end_period_ts"]), None)
    if now is not None:
        bid, ask = _price(now, "yes_bid", "open"), _price(now, "yes_ask", "open")
    if bid is None or ask is None:
        before = [c for c in candles if c["end_period_ts"] <= ts]
        if before:
            bid, ask = _price(before[-1], "yes_bid", "close"), _price(before[-1], "yes_ask", "close")
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None
    return (bid + ask) / 2


def combo_fair_yes(legs: list[dict], leg_mids: dict[str, float | None]) -> float | None:
    """Fair YES price of a combo: product of each leg's chance, legs assumed independent."""
    p = 1.0
    for l in legs:
        m = leg_mids.get(l["market_ticker"])
        if m is None:
            return None
        p *= m if l["side"] == "yes" else 1 - m
    return p


def score_fill(f: Fill, fair_yes: float | None, result: str | None) -> dict:
    """EV (vs the book when traded), variance and, once settled, PnL of one fill, after taker fees."""
    fair = None if fair_yes is None else (fair_yes if f.side == "yes" else 1 - fair_yes)
    fee = f.fee
    out = {"fair": fair, "fee": fee, "ev": None, "var": None, "pnl": None}
    if fair is not None:
        out["ev"] = (fair - f.price) * f.count - fee
        out["var"] = f.count ** 2 * fair * (1 - fair)
    if result in ("yes", "no"):
        out["pnl"] = ((1.0 if result == f.side else 0.0) - f.price) * f.count - fee
    return out


def summarize(fills: list[Fill], fair_yes: dict[str, float | None], results: dict[str, str | None]) -> dict:
    """Totals for a bettor's sample of fills.

    `edge` and `z` compare realized PnL with EV over the fills that are both settled
    and priced, so luck and skill are measured on the same bets: z = (PnL - EV) / SD.
    A z of 2+ over hundreds of fills is hard to get by luck.
    """
    s = defaultdict(float)
    markets = set()
    first = last = None
    for f in fills:
        sc = score_fill(f, fair_yes.get(f.trade_id), results.get(f.ticker))
        markets.add(f.ticker)
        s["n"] += 1
        s["vol"] += f.stake
        s["fees"] += sc["fee"]
        s["taker"] += f.taker
        first = f.ts if first is None else min(first, f.ts)
        last = f.ts if last is None else max(last, f.ts)
        if sc["ev"] is not None:
            s["ev"] += sc["ev"]
            s["ev_n"] += 1
            s["ev_vol"] += f.stake
        if sc["pnl"] is not None:
            s["pnl"] += sc["pnl"]
            s["pnl_n"] += 1
            s["pnl_vol"] += f.stake
            s["wins"] += sc["pnl"] > 0
            if sc["ev"] is not None:
                s["both_pnl"] += sc["pnl"]
                s["both_ev"] += sc["ev"]
                s["both_var"] += sc["var"]
        else:
            s["open_vol"] += f.stake
    n = s["n"]
    edge = s["both_pnl"] - s["both_ev"]
    return {
        "n": int(n), "markets": len(markets), "vol": s["vol"], "fees": s["fees"],
        "taker_share": s["taker"] / n if n else None,
        "ev": s["ev"] if s["ev_n"] else None, "ev_cover": s["ev_n"] / n if n else None,
        "ev_roi": s["ev"] / s["ev_vol"] if s["ev_vol"] else None,
        "pnl": s["pnl"] if s["pnl_n"] else None, "settled": int(s["pnl_n"]),
        "win_rate": s["wins"] / s["pnl_n"] if s["pnl_n"] else None,
        "pnl_roi": s["pnl"] / s["pnl_vol"] if s["pnl_vol"] else None,
        "open_vol": s["open_vol"],
        "edge": edge if s["both_var"] else None,
        "z": edge / math.sqrt(s["both_var"]) if s["both_var"] > 0 else None,
        "first": first, "last": last,
    }


def _money(metrics: dict, key: str) -> float | None:
    v = metrics.get(key)
    return None if v is None else v / MONEY_UNIT


@dataclass
class Profile:
    nickname: str
    # "feed" (busy in the tennis feed), "you" (added on the page), "rfq" (found through RFQ matching)
    sources: set[str] = field(default_factory=set)
    hidden: bool | None = None  # trades hidden on their profile; None = not checked yet
    missing: bool = False  # no profile with this nickname
    fills: dict[str, Fill] = field(default_factory=dict)
    back_cursor: str | None = None  # where paging older history resumes; None once it's all loaded
    history_done: bool = False
    metrics: dict = field(default_factory=dict)
    metrics_30d: dict = field(default_factory=dict)
    metrics_at: float = 0.0
    history_at: float = 0.0
    code: str | None = None  # set for a bettor known only by their RFQ creator ID
    linked: str | None = None  # for a code: the public nickname its trades showed
    sure: int = 0  # for a code: matches where every RFQ that fit was theirs
    link_tried: set[str] = field(default_factory=set)  # trade ids already looked up in the social feed
    vol: float = 0.0  # for a code: dollars staked in its matched bets
    tennis_vol: float = 0.0  # for a code: of vol, on combos with a tennis leg
    last_bet: float = 0.0  # for a code: when its newest matched bet was


@dataclass
class BettorSnapshot:
    rows: list[dict]
    updated: datetime
    status: str
    feed_since: datetime | None
    feed_trades: int
    named_trades: int
    errors: list[str] = field(default_factory=list)
    rfq: dict | None = None  # RFQ matching status, when a key is set


class BettorTracker:
    """Background worker that keeps a ranked table of bettors fresh.

    One cycle every `every` seconds: read new tennis trades from the social feed,
    update each candidate's history and metrics, look up market results, price new
    fills against one-minute candles, and publish rows.
    """

    def __init__(self, client, series: list[str] | None = None, feed_hours: float = 24, max_fills: int = 50_000,
                 pages_per_pass: int = 60, price_days: int = 30, top_n: int = 40, every: float = 900, rfq=None,
                 links_per_cycle: int = 60, top_codes: int = 100, top_tennis_codes: int = 50, code_min_bets: int = 3,
                 max_codes: int = 30_000, max_code_fills: int = 5_000,
                 first_pages: int = 30, more_pages: int = 20, metrics_ttl: float = 3600, history_ttl: float = 1800):
        self.client = client
        self.series = TENNIS_SERIES if series is None else series  # [] = no feed discovery
        self.feed_window = feed_hours * 3600
        self.rfq = rfq  # an rfq.RfqBook when a Kalshi API key is set
        self.links_per_cycle = links_per_cycle
        # Thousands of people bet combos every hour. Every code keeps its bets, but only the biggest
        # (and the biggest in tennis) with a few bets get priced, settled and listed.
        self.top_codes_n = top_codes
        self.top_tennis_codes = top_tennis_codes
        self.code_min_bets = code_min_bets
        self.max_codes = max_codes  # a memory guard: past this, the smallest idle codes are dropped
        self.max_code_fills = max_code_fills
        self.max_fills = max_fills  # a memory guard; real histories are usually far smaller
        self.pages_per_pass = pages_per_pass  # long histories load over several cycles
        self.price_days = price_days  # fills older than this aren't priced against the book
        self.top_n = top_n
        self.every = every
        self.first_pages = first_pages
        self.more_pages = more_pages
        self.metrics_ttl = metrics_ttl
        self.history_ttl = history_ttl

        self.lock = threading.Lock()
        self._rfq_lock = threading.Lock()  # any session's run can attach RFQ matching (use_key)
        self.feed: list[tuple[float, str, float, str]] = []  # (ts, nickname, stake, ticker), named sides only
        self.feed_newest: dict[str, float] = {}
        self.feed_oldest: float | None = None
        self.feed_trades = 0
        self.feed_named = 0  # of feed_trades, ones with at least one public nickname
        self.profiles: dict[str, Profile] = {}
        self.wanted: list[str] = []  # nicknames added from the page, analyzed first
        self.markets: dict[str, dict] = {}  # ticker -> {result, final, title, legs}
        self.fair: dict[str, float | None] = {}  # trade_id -> fair YES at trade time
        self.tennis_hint: set[str] = set()  # combo tickers the RFQ stream saw with a tennis leg
        self.snapshot: BettorSnapshot | None = None
        self.status = "starting"
        self.errors: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ---- discovery ----
    def read_feed(self) -> None:
        cutoff = time.time() - self.feed_window
        for series in self.series:
            newest = self.feed_newest.get(series)
            pages = self.first_pages if newest is None else self.more_pages
            cursor, top = None, newest
            for _ in range(pages):
                data = self.client.social_trades(series_ticker=series, cursor=cursor)
                trades = data.get("trades") or []
                done = not trades
                for t in trades:
                    ts = _ts(t["create_date"])
                    if (newest is not None and ts <= newest) or ts < cutoff:
                        done = True
                        break
                    top = ts if top is None else max(top, ts)
                    self.feed_trades += 1
                    self.feed_named += bool(t.get("taker_nickname") or t.get("maker_nickname"))
                    yes = float(t["price_dollars"])
                    count = float(t["count_fp"])
                    with self.lock:
                        if t.get("taker_nickname"):
                            stake = count * (yes if t["taker_side"] == "yes" else 1 - yes)
                            self.feed.append((ts, t["taker_nickname"], stake, t["ticker"]))
                        if t.get("maker_nickname"):
                            stake = count * (1 - yes if t["taker_side"] == "yes" else yes)
                            self.feed.append((ts, t["maker_nickname"], stake, t["ticker"]))
                    self.feed_oldest = ts if self.feed_oldest is None else min(self.feed_oldest, ts)
                cursor = data.get("cursor")
                if done or not cursor:
                    break
            if top is not None:
                self.feed_newest[series] = top
        with self.lock:
            self.feed = [x for x in self.feed if x[0] >= cutoff]
        if self.feed_oldest is not None:
            self.feed_oldest = max(self.feed_oldest, cutoff)

    def feed_stats(self) -> dict[str, dict]:
        out: dict[str, dict] = defaultdict(lambda: {"n": 0, "vol": 0.0, "last": 0.0})
        with self.lock:
            for ts, nick, stake, _ in self.feed:
                s = out[nick]
                s["n"] += 1
                s["vol"] += stake
                s["last"] = max(s["last"], ts)
        return dict(out)

    def add(self, nickname: str) -> None:
        """Analyze a nickname typed on the page, ahead of the next scheduled cycle."""
        nickname = nickname.strip().lstrip("@")
        if not nickname:
            return
        with self.lock:
            p = self.profiles.setdefault(nickname, Profile(nickname))
            p.sources.add("you")
            if nickname not in self.wanted:
                self.wanted.append(nickname)
        self._wake.set()

    def candidates(self) -> list[Profile]:
        stats = self.feed_stats()
        top = sorted((n for n, s in stats.items() if s["n"] >= 3), key=lambda n: -stats[n]["vol"])[: self.top_n]
        for p in self.profiles.values():
            p.sources.discard("feed")
        for n in top:
            self.profiles.setdefault(n, Profile(n)).sources.add("feed")
        with self.lock:
            wanted = list(self.wanted)
        order = wanted + top + [n for n, p in self.profiles.items()
                                if not p.code and p.sources & {"you", "rfq"}]
        return [self.profiles[n] for n in dict.fromkeys(order) if n in self.profiles]

    # ---- RFQ codes ----
    def sync_codes(self) -> int:
        """Turn new RFQ matches into per-code fills (the requester took the trade). Returns how many."""
        if self.rfq is None:
            return 0
        matches = self.rfq.take()
        for m in matches:
            e = m.execution
            key = "#" + m.creator[:10]
            p = self.profiles.get(key)
            if p is None:
                p = self.profiles[key] = Profile(key, sources={"rfq"}, hidden=False, history_done=True, code=m.creator)
            if e.trade_ids[0] in p.fills:
                continue
            p.fills[e.trade_ids[0]] = Fill(e.trade_ids[0], e.ts, e.ticker, e.side, e.price, e.count, True)
            p.sure += m.sure
            p.vol += e.price * e.count
            p.last_bet = max(p.last_bet, e.ts)
            if m.tennis:
                p.tennis_vol += e.price * e.count
                self.tennis_hint.add(e.ticker)
            if len(p.fills) > self.max_code_fills:
                del p.fills[next(iter(p.fills))]  # oldest
        self._cap_codes()
        return len(matches)

    def _cap_codes(self) -> None:
        codes = self.code_profiles()
        if len(codes) <= self.max_codes:
            return
        keep = {p.nickname for p in self.top_codes()}
        idle = time.time() - 3600
        drop = sorted((p for p in codes if p.nickname not in keep and not p.linked),
                      key=lambda p: (p.last_bet >= idle, p.vol))
        for p in drop[: len(codes) - int(self.max_codes * 0.8)]:
            del self.profiles[p.nickname]

    def top_codes(self) -> list[Profile]:
        """The codes worth analyzing: the biggest by dollars bet, and the biggest in tennis."""
        codes = [p for p in self.code_profiles() if len(p.fills) >= self.code_min_bets]
        by_vol = sorted(codes, key=lambda p: -p.vol)[: self.top_codes_n]
        tennis = sorted((p for p in codes if p.tennis_vol), key=lambda p: -p.tennis_vol)[: self.top_tennis_codes]
        return list({p.nickname: p for p in by_vol + tennis}.values())

    def link_codes(self) -> None:
        """Find which public nickname, if any, is behind each code.

        A matched trade that shows a taker nickname in the social feed names the
        code's owner. First check histories we already have, then ask the social
        feed about a few of each unlinked code's trades (biggest codes first).
        """
        takers = {f.trade_id: p.nickname for p in self.profiles.values() if not p.code
                  for f in p.fills.values() if f.taker}
        codes = sorted((p for p in self.top_codes() if not p.linked), key=lambda p: -p.vol)
        budget = self.links_per_cycle
        for p in codes:
            name = next((takers[t] for t in p.fills if t in takers), None)
            tried = p.link_tried
            for f in sorted(p.fills.values(), key=lambda f: -f.stake):
                if name or budget <= 0:
                    break
                if f.trade_id in tried:
                    continue
                tried.add(f.trade_id)
                budget -= 1
                data = self.client.social_trades(market_ticker=f.ticker)
                hit = next((t for t in data.get("trades") or [] if t["trade_id"] == f.trade_id), None)
                if hit and hit.get("taker_nickname"):
                    name = hit["taker_nickname"]
                if len(tried) >= 3:  # three anonymous trades in a row: probably a private profile
                    break
            if name:
                p.linked = name
                prof = self.profiles.setdefault(name, Profile(name))
                prof.sources.add("rfq")

    def code_profiles(self) -> list[Profile]:
        return [p for p in list(self.profiles.values()) if p.code]

    def _prune_caches(self) -> None:
        """Forget prices and markets of fills no bettor holds any more (dropped codes)."""
        fills = [f for p in list(self.profiles.values()) for f in list(p.fills.values())]
        ids = {f.trade_id for f in fills}
        tickers = {f.ticker for f in fills}
        tickers |= {l["market_ticker"] for t in list(tickers) for l in self.markets.get(t, {}).get("legs", ())}
        self.fair = {k: v for k, v in self.fair.items() if k in ids}
        self.markets = {k: v for k, v in self.markets.items() if k in tickers}
        self.tennis_hint &= tickers

    # ---- per-bettor data ----
    def refresh_metrics(self, p: Profile, force: bool = False) -> None:
        if p.code or p.missing or (not force and time.time() - p.metrics_at < self.metrics_ttl):
            return
        try:
            p.metrics = self.client.profile_metrics(p.nickname)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                p.missing = True
                return
            raise
        p.metrics_30d = self.client.profile_metrics(p.nickname, days=30)
        p.metrics_at = time.time()

    def refresh_history(self, p: Profile, force: bool = False) -> None:
        """Load the bettor's whole trade history, newest first.

        Each pass first pages new trades until it reaches ones we have, then keeps
        paging back in time from where the last pass stopped, up to pages_per_pass
        pages, so a long history fills in over a few cycles.
        """
        if p.code or p.hidden or p.missing or (not force and time.time() - p.history_at < self.history_ttl):
            return
        budget = self.pages_per_pass

        def page(cursor):
            nonlocal budget
            budget -= 1
            data = self.client.social_trades(nickname=p.nickname, cursor=cursor)
            if data.get("visibility_state") == "hidden":
                p.hidden = True
                return None, []
            p.hidden = False
            fills = [f for f in (to_fill(t, p.nickname) for t in data.get("trades") or []) if f is not None]
            return data.get("cursor") or None, fills

        if p.fills:
            newest = max(f.ts for f in p.fills.values())
            cursor = None
            for _ in range(self.pages_per_pass):  # new trades; usually one page
                cursor, fills = page(cursor)
                if p.hidden:
                    return
                p.fills.update((f.trade_id, f) for f in fills)
                if not cursor or not fills or fills[-1].ts < newest - 60:
                    break
            budget = self.pages_per_pass
        else:
            p.back_cursor, fills = page(None)
            if p.hidden:
                return
            p.fills.update((f.trade_id, f) for f in fills)
            p.history_done = p.back_cursor is None
        while budget > 0 and p.back_cursor and len(p.fills) < self.max_fills:
            p.back_cursor, fills = page(p.back_cursor)
            p.fills.update((f.trade_id, f) for f in fills)
            if not fills:
                p.back_cursor = None
        p.history_done = p.back_cursor is None
        p.history_at = time.time()

    def refresh_markets(self, profiles: list[Profile]) -> None:
        tickers = {f.ticker for p in profiles for f in p.fills.values()}
        need = [t for t in tickers if not self.markets.get(t, {}).get("final")]
        found = self.client.get_markets(need, batch_size=100) if need else {}
        legs_needed = set()
        for t, m in found.items():
            legs = m.get("mve_selected_legs") or []
            result = m.get("result") or None
            self.markets[t] = {
                "result": result if result in ("yes", "no") else None,
                "final": m.get("status") in ("finalized", "settled") and result in ("yes", "no"),
                "title": m.get("yes_sub_title") or m.get("title") or t,
                "event": m.get("event_ticker", ""),
                "legs": [{"market_ticker": l["market_ticker"], "side": l["side"], "event_ticker": l.get("event_ticker", "")}
                         for l in legs],
            }
            legs_needed.update(l["market_ticker"] for l in legs)
        missing_legs = [t for t in legs_needed if t not in self.markets]
        for t, m in (self.client.get_markets(missing_legs, batch_size=100) if missing_legs else {}).items():
            result = m.get("result") or None
            self.markets[t] = {"result": result if result in ("yes", "no") else None,
                               "final": m.get("status") in ("finalized", "settled") and result in ("yes", "no"),
                               "title": m.get("yes_sub_title") or m.get("title") or t,
                               "event": m.get("event_ticker", ""), "legs": []}

    def refresh_fair(self, profiles: list[Profile], lookback: int = 3600) -> None:
        """Price fills we haven't priced yet, an hour of trades per candle request."""
        since = time.time() - self.price_days * DAY
        todo = [f for p in profiles for f in p.fills.values() if f.ts >= since and f.trade_id not in self.fair]
        buckets: dict[int, list[Fill]] = defaultdict(list)
        for f in todo:
            buckets[int(f.ts // 3600)].append(f)
        for hour, fills in sorted(buckets.items(), reverse=True):
            needs: dict[str, list[dict]] = {}
            for f in fills:
                if not f.ticker.startswith(COMBO_PREFIX):
                    needs[f.trade_id] = [{"market_ticker": f.ticker, "side": "yes"}]
                elif f.ticker in self.markets:  # else wait until its legs are known
                    needs[f.trade_id] = self.markets[f.ticker]["legs"]
            tickers = sorted({l["market_ticker"] for legs in needs.values() for l in legs})
            try:
                candles = self.client.get_candles(tickers, hour * 3600 - lookback, hour * 3600 + 3600) if tickers else {}
            except Exception as exc:  # try this hour again next cycle
                with self.lock:
                    self.errors.append(f"candles: {exc!r}"[:200])
                continue
            by_id = {f.trade_id: f for f in fills}
            for trade_id, legs in needs.items():
                f = by_id[trade_id]
                mids = {l["market_ticker"]: mid_at(candles.get(l["market_ticker"], []), f.ts) for l in legs}
                self.fair[trade_id] = combo_fair_yes(legs, mids) if legs else None

    # ---- rows ----
    def build_rows(self) -> list[dict]:
        feed = self.feed_stats()
        results = {t: m["result"] for t, m in self.markets.items()}
        month = time.time() - 30 * DAY
        codes_of: dict[str, list[str]] = defaultdict(list)
        for p in list(self.profiles.values()):
            if p.code and p.linked:
                codes_of[p.linked].append(p.nickname)
        shown_codes = {p.nickname for p in self.top_codes()}
        rows = []
        for p in list(self.profiles.values()):
            if not p.sources or (p.missing and "you" not in p.sources):
                continue
            if p.code and (p.nickname not in shown_codes or (p.linked and p.linked in self.profiles)):
                continue  # a small code, or shown on its nickname's row
            fills = sorted(p.fills.values(), key=lambda f: -f.ts)
            tennis = [f for f in fills if f.ticker in self.tennis_hint
                      or is_tennis(f.ticker, self.markets.get(f.ticker, {}).get("legs"))]
            fs = feed.get(p.nickname, {})
            recent = []
            for f in fills[:15]:
                sc = score_fill(f, self.fair.get(f.trade_id), results.get(f.ticker))
                m = self.markets.get(f.ticker, {})
                recent.append({"t": f.ts, "tk": f.ticker, "title": m.get("title") or f.ticker,
                               "legs": len(m.get("legs") or []), "side": f.side, "price": f.price, "n": f.count,
                               "taker": f.taker, "fair": sc["fair"], "ev": sc["ev"], "pnl": sc["pnl"]})
            rows.append({
                "nick": p.nickname, "src": sorted(p.sources), "hidden": p.hidden, "missing": p.missing,
                "pnl_all": _money(p.metrics, "pnl"), "pnl_30d": _money(p.metrics_30d, "pnl"),
                "markets_all": p.metrics.get("num_markets_traded"), "markets_30d": p.metrics_30d.get("num_markets_traded"),
                "invested": _money(p.metrics, "dollars_investment"),
                "feed_n": fs.get("n", 0), "feed_vol": fs.get("vol", 0.0), "feed_last": fs.get("last") or None,
                "all": summarize(fills, self.fair, results),
                "tennis": summarize(tennis, self.fair, results),
                "all30": summarize([f for f in fills if f.ts >= month], self.fair, results),
                "tennis30": summarize([f for f in tennis if f.ts >= month], self.fair, results),
                "history_done": p.history_done,
                "code": p.code, "linked": p.linked, "codes": codes_of.get(p.nickname, []), "sure": p.sure,
                "recent": recent,
            })
            if p.code:
                # No Kalshi profile numbers for a code: its PnL is what its matched bets made.
                rows[-1]["pnl_all"] = rows[-1]["all"]["pnl"]
                rows[-1]["pnl_30d"] = rows[-1]["all30"]["pnl"]
        return rows

    def publish(self) -> None:
        with self.lock:
            errors = list(self.errors[-5:])
        snap = BettorSnapshot(
            rows=self.build_rows(), updated=datetime.now(timezone.utc), status=self.status,
            feed_since=None if self.feed_oldest is None else datetime.fromtimestamp(self.feed_oldest, timezone.utc),
            feed_trades=self.feed_trades, named_trades=self.feed_named, errors=errors,
            rfq=None if self.rfq is None else {**self.rfq.stats, "stream": self.rfq.ws_status,
                                                "error": self.rfq.last_error, "since": self.rfq.started,
                                                "codes": len(self.code_profiles()), "shown": len(self.top_codes()),
                                                "linked": sum(1 for p in self.code_profiles() if p.linked)})
        self.snapshot = snap

    # ---- loop ----
    def _step(self, label: str, fn, *args) -> None:
        self.status = label
        try:
            fn(*args)
        except Exception as exc:  # one bad call shouldn't stop the cycle
            with self.lock:
                self.errors.append(f"{label}: {exc!r}"[:200])

    def analyze(self, profiles: list[Profile], force: bool = False) -> None:
        for i, p in enumerate(profiles):
            self._step(f"profiles {i + 1}/{len(profiles)}", self.refresh_metrics, p, force)
            self._step(f"profiles {i + 1}/{len(profiles)}", self.refresh_history, p, force)
            if i % 10 == 9:
                self.publish()
        self._step("market results", self.refresh_markets, profiles)
        self.publish()
        self._step("pricing fills", self.refresh_fair, profiles)

    def cycle(self) -> None:
        with self.lock:
            self.errors.clear()
        self._step("reading tennis trades", self.read_feed)
        self._step("matching RFQs", self.sync_codes)
        self._step("naming RFQ codes", self.link_codes)
        self.publish()
        self.analyze(self.candidates() + self.top_codes())
        self._step("cleaning up", self._prune_caches)
        self.status = "idle"
        self.publish()

    def serve_wanted(self) -> None:
        with self.lock:
            wanted, self.wanted = list(self.wanted), []
        profiles = [self.profiles[n] for n in wanted if n in self.profiles]
        if profiles:
            self.analyze(profiles, force=True)
            self.status = "idle"
            self.publish()

    def run_forever(self) -> None:
        next_cycle = 0.0
        while not self._stop.is_set():
            activity.wait_active(self._stop)  # no Kalshi calls while nobody has the app open
            if self._stop.is_set():
                break
            try:
                if time.time() >= next_cycle:
                    self.cycle()
                    next_cycle = time.time() + self.every
                else:
                    self.serve_wanted()
                    if self.rfq is not None and self.sync_codes():
                        self.publish()
            except Exception as exc:
                with self.lock:
                    self.errors.append(repr(exc)[:200])
            self._wake.wait(max(1.0, min(60.0, next_cycle - time.time())))
            self._wake.clear()

    def use_key(self, auth, make_book) -> None:
        """Start RFQ matching with `auth`, or switch the running book to it. Pages call this on every
        run, so a key saved after the app started works without a reboot."""
        with self._rfq_lock:
            if self.rfq is None:
                self.rfq = make_book(auth).start()
            elif self.rfq.client.auth is not auth:
                self.rfq.client.auth = auth  # the stream signs its next reconnect with it

    def start(self) -> "BettorTracker":
        if self._thread is None:
            self._thread = threading.Thread(target=self.run_forever, daemon=True, name=THREAD_NAME)
            self._thread.stop = self.stop
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
