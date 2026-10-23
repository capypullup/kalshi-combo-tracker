"""Put a stable ID on combo bettors by matching RFQs to executed combo trades.

Every combo bet on Kalshi starts as an RFQ (request for quote). Kalshi broadcasts
each RFQ to every logged-in user with the requester's `creator_id`: a salted hash
that stays the same for that user across RFQs. The public trade feed has the
executed combo trades, but no trader ID.

When the requester accepts a quote, the trade prints and Kalshi closes the RFQ in
the same instant, and the close message carries the market, the size and the
requester's ID. So for each combo trade we look for an RFQ on the same market, of
the same size (N contracts, or $X of cost), that closed within a couple of seconds
of the trade. Most RFQs close without trading: the Kalshi app asks again every few
seconds while a bet slip is open. Those repeats come from the same person, so:

- "sure": every RFQ that fits the trade (and closed about as near to it) is from
  the same requester;
- "likely": RFQs from several requesters fit, and the one that closed nearest the
  trade wins.

A maker's quote can fill a bit less than the requested size, so when no RFQ fits
exactly, one that asked for somewhat more (up to 2.5x) counts too. Two bets on the
same combo a moment apart print as one group of fills; if the group fits nothing,
each fill is matched on its own.

The requester accepts a maker's quote, so the requester is the taker: their side
is the trade's taker side.

At busy times Kalshi sends about 3,000 RFQs a second (twice that in messages, with
the closes). So the stream thread skips the RFQs themselves and keeps only the last
minute of closes, and a second thread polls the trade feed every few seconds and
matches new combo trades while their RFQ's close is still in memory.

Requesters can choose to hide their ID (everyone then sees one shared placeholder
hash), but Kalshi reveals the real ID once the RFQ executes, so we look those up
after matching.

This needs a Kalshi API key: the RFQ feed isn't public.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .bettors import is_tennis
from . import activity
from .client import COMBO_PREFIX
from .engine import taker_fee

# sha256("") — what everyone sees as creator_id while a requester hides theirs.
PLACEHOLDER = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
THREAD_NAME = "rfq-stream"
MATCHER_NAME = "rfq-matcher"
BEFORE, AFTER = 2.0, 3.0  # an RFQ that filled closed this many seconds before / after its trade
TENNIS_HINTS = ('"KXATP', '"KXWTA', '"KXITF')  # cheap check before parsing an RFQ's legs

# A closed RFQ: (closed at, creator, contracts, target cost in dollars, RFQ id if the creator is hidden)
Close = tuple
STATS = ("rfqs", "closes", "executions", "sure", "likely", "partial", "split", "unmatched", "hidden",
         "revealed", "unrevealed", "hidden_rfqs", "hidden_closes", "overflows", "resubscribes")


def _ts(value) -> float | None:
    """Kalshi sends times as ISO strings, or as epoch seconds / milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Execution:
    """One or more combo fills on a market, same side, within a second or so."""

    ticker: str
    side: str
    ts: float  # first fill
    count: float
    price: float  # avg price per contract of `side`
    trade_ids: list[str]
    end: float = 0.0  # last fill
    parts: list[Execution] = field(default_factory=list)  # each fill on its own, when there are several


@dataclass(slots=True)
class Match:
    rfq_id: str  # kept only for hidden requesters, to look up their real ID
    creator: str
    execution: Execution
    sure: bool  # every RFQ that fit was from this requester
    tennis: bool = False  # the combo has a tennis leg
    lag: float = 0.0  # seconds from the trade to the RFQ's close
    partial: bool = False  # the RFQ asked for more than filled


def executions(trades: list[dict], gap: float = 1.5) -> list[Execution]:
    """Group raw combo trades (v2 feed shape) into executions."""
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        side = t.get("taker_outcome_side") or t["taker_side"]
        by_key[(t["ticker"], side)].append(t)
    out = []
    for (ticker, side), ts in by_key.items():
        ts.sort(key=lambda t: _ts(t["created_time"]))
        group: list[dict] = []
        for t in ts + [None]:
            if group and (t is None or _ts(t["created_time"]) - _ts(group[-1]["created_time"]) > gap):
                count = sum(float(g["count_fp"]) for g in group)
                key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
                price = sum(float(g[key]) * float(g["count_fp"]) for g in group) / count if count else 0.0
                parts = [Execution(ticker, side, _ts(g["created_time"]), float(g["count_fp"]), float(g[key]),
                                   [g["trade_id"]], _ts(g["created_time"])) for g in group] if len(group) > 1 else []
                out.append(Execution(ticker, side, _ts(group[0]["created_time"]), count, price,
                                     [g["trade_id"] for g in group], _ts(group[-1]["created_time"]), parts))
                group = []
            if t is not None:
                group.append(t)
    return out


def size_fits(contracts: float | None, target: float | None, ex: Execution, cost_tol: float = 0.03) -> bool:
    """Does this execution have the size the RFQ asked for?"""
    if contracts:
        return abs(ex.count - contracts) <= max(0.011, 0.001 * contracts)
    if target:
        principal = ex.count * ex.price
        tol = max(0.05, cost_tol * target)  # contracts come in 0.01 steps, so cost misses by about a tick
        # A target cost usually includes the taker fee; some requests ask for the cost before fees.
        return abs(principal + taker_fee(ex.price, ex.count) - target) <= tol or abs(principal - target) <= tol
    return False


def partly_fits(contracts: float | None, target: float | None, ex: Execution, least: float = 0.4,
                cost_tol: float = 0.03) -> bool:
    """Is this execution part of what the RFQ asked for? (A quote can fill a bit less.)"""
    if contracts:
        return least * contracts <= ex.count <= contracts + max(0.011, 0.001 * contracts)
    if target:
        principal = ex.count * ex.price
        return least * target <= principal + taker_fee(ex.price, ex.count) and principal <= target * (1 + cost_tol) + 0.05
    return False


def pick(closes, ex: Execution, used=()) -> tuple[Close | None, bool, bool]:
    """The RFQ close that fits this execution best, whether the requester is sure, and whether the size was exact."""
    near = [c for c in closes if ex.ts - BEFORE <= c[0] <= ex.ts + AFTER]
    for fits_size in (size_fits, partly_fits):
        fits = [c for c in near if fits_size(c[2], c[3], ex)]
        free = [c for c in fits if (ex.ticker, c[0]) not in used]
        if not free:
            continue
        best = min(free, key=lambda c: abs(c[0] - ex.ts))
        # The RFQ that filled closes within a fraction of a second of its trade, so a close well
        # after or before that (someone else's slip refreshing) doesn't make the match doubtful.
        rivals = [c for c in fits if abs(c[0] - ex.ts) <= abs(best[0] - ex.ts) + 0.5]
        creators = {c[1] for c in rivals}
        # Hidden requesters all share the placeholder, so only a lone fit tells which RFQ (and ID) it was.
        sure = len(rivals) == 1 or (len(creators) == 1 and PLACEHOLDER not in creators)
        return best, sure, fits_size is size_fits
    return None, False, False


class RfqBook:
    """Listens to Kalshi's RFQ stream and names the requester behind each new combo trade.

    One thread reads the websocket and keeps the last `keep_seconds` of RFQ closes;
    another polls the trade feed every `poll_seconds` and matches new combo trades.
    New matches wait in `take()` for the bettor tracker.
    """

    def __init__(self, client, keep_seconds: float = 30.0, poll_seconds: float = 5.0, settle: float = 3.0,
                 wait: float = 12.0, reveal_per_poll: int = 8, max_out: int = 200_000):
        self.client = client
        self.keep = keep_seconds
        self.poll_seconds = poll_seconds
        self.settle = settle  # wait this long after a trade so every fill of its execution is in
        self.wait = wait  # keep trying an unmatched execution this long (its close can arrive late)
        self.reveal_per_poll = reveal_per_poll
        self.lock = threading.Lock()
        self.closes: dict[str, list[Close]] = defaultdict(list)  # combo ticker -> closes, oldest first
        self.used: dict[tuple, float] = {}  # (ticker, closed at) of closes already matched to a trade
        self.tennis: dict[str, float] = {}  # combo tickers with a tennis leg -> last requested at
        self.out: deque[Match] = deque(maxlen=max_out)  # matches not yet taken
        self.recent: deque[Match] = deque(maxlen=200)
        self.missed: deque[tuple] = deque(maxlen=200)  # (execution, nearest close on its market or None)
        self.stats: dict[str, int] = dict.fromkeys(STATS, 0)  # every key up front: two threads update it
        self.ws_status = "not started"
        self.last_error: str | None = None
        self.started: float | None = None
        self._fresh: list[dict] = []  # combo trades too new to group yet
        self._waiting: list[Execution] = []  # executions with no fitting close yet
        self._seen: dict[str, float] = {}  # trade ids already read -> trade time
        self._since: float | None = None
        self._reveal: deque[list] = deque(maxlen=5000)  # [match, tries, next try at]
        self._cmd = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- intake (stream thread) ----
    def add_close(self, ticker: str, ts: float, creator: str, contracts: float | None, target: float | None,
                  rfq_id: str | None = None) -> None:
        creator = sys.intern(creator or PLACEHOLDER)
        close = (ts, creator, contracts or None, target or None, rfq_id if creator == PLACEHOLDER else None)
        with self.lock:
            self.closes[ticker].append(close)
        self.stats["closes"] += 1
        if creator == PLACEHOLDER:
            self.stats["hidden_closes"] += 1

    def _note_tennis(self, body: dict) -> None:
        ticker = body.get("market_ticker")
        if ticker and is_tennis(ticker, body.get("mve_selected_legs")):
            with self.lock:
                self.tennis[ticker] = time.time()

    def on_message(self, raw: str) -> str | None:
        """Handle one stream message. Returns "resubscribe" when Kalshi dropped the subscription."""
        if raw.startswith('{"type":"rfq_created"'):  # most messages: count them, don't parse them
            self.stats["rfqs"] += 1
            if PLACEHOLDER in raw:
                self.stats["hidden_rfqs"] += 1
            if any(h in raw for h in TENNIS_HINTS):
                self._note_tennis(json.loads(raw).get("msg") or {})
            return None
        msg = json.loads(raw)
        kind, body = msg.get("type"), msg.get("msg") or {}
        if kind == "rfq_deleted":
            ts = _ts(body.get("deleted_ts"))
            if ts is not None and body.get("market_ticker"):
                self.add_close(body["market_ticker"], ts, body.get("creator_id"), _float(body.get("contracts_fp")),
                               _float(body.get("target_cost_dollars")), body.get("id"))
        elif kind == "rfq_created":
            self.stats["rfqs"] += 1
            self._note_tennis(body)
        elif kind == "error":
            self.last_error = f"stream: {body}"[:200]
            if body.get("code") == 25:  # "Subscription buffer overflow": we fell behind
                self.stats["overflows"] += 1
        elif kind == "unsubscribed":
            return "resubscribe"
        return None

    # ---- websocket ----
    def _subscribe(self, ws) -> None:
        self._cmd += 1
        ws.send(json.dumps({"id": self._cmd, "cmd": "subscribe", "params": {"channels": ["communications"]}}))
        self.ws_status = "connected"

    def run_stream(self) -> None:
        import websocket  # websocket-client

        backoff = 1.0
        while not self._stop.is_set():
            if not activity.active():  # hang up while nobody has the app open
                self.ws_status = "paused"
                activity.wait_active(self._stop)
                continue
            try:
                headers = self.client.auth.headers("GET", "/trade-api/ws/v2")
                # websocket-client's own UTF-8 check is pure Python and caps reading at ~8k messages a
                # second; Kalshi sends ~6k at busy times and drops a subscriber that falls behind.
                ws = websocket.create_connection(WS_URL, header=[f"{k}: {v}" for k, v in headers.items()],
                                                 timeout=30, skip_utf8_validation=True, enable_multithread=False)
                self._subscribe(ws)
                backoff = 1.0
                while not self._stop.is_set() and activity.active():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping()
                        continue
                    if raw and self.on_message(raw) == "resubscribe":
                        self.stats["resubscribes"] += 1
                        self._subscribe(ws)
                ws.close()
            except Exception as exc:  # reconnect after network errors
                self.ws_status = "reconnecting"
                self.last_error = f"stream: {exc!r}"[:200]
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    # ---- matching (matcher thread) ----
    def match_trades(self, trades: list[dict], now: float | None = None) -> list[Match]:
        """Group new combo trades into executions and match each to the RFQ that closed with it."""
        now = time.time() if now is None else now
        self._fresh.extend(trades)
        ready, held = [], []
        for e in executions(self._fresh):
            (ready if e.end <= now - self.settle else held).append(e)
        held_ids = {tid for e in held for tid in e.trade_ids}
        self._fresh = [t for t in self._fresh if t["trade_id"] in held_ids]
        self.stats["executions"] += len(ready)
        found, waiting = [], []
        with self.lock:
            for e in sorted(self._waiting + ready, key=lambda e: e.ts):
                matches = self._match(e)
                if matches:
                    found += matches
                elif now - e.end < self.wait:
                    waiting.append(e)
                else:
                    self.stats["unmatched"] += 1
                    near = min(self.closes.get(e.ticker, ()), key=lambda c: abs(c[0] - e.ts), default=None)
                    self.missed.append((e, near))
        self._waiting = waiting
        for m in found:
            self.stats["sure" if m.sure else "likely"] += 1
            self.stats["partial"] += m.partial
            self.recent.append(m)
            if m.creator == PLACEHOLDER:
                self.stats["hidden"] += 1
                self._reveal.append([m, 0, now])
            else:
                self.out.append(m)
        return found

    def _match(self, e: Execution) -> list[Match]:
        """Match one execution (call with the lock held). A group of fills that fits no RFQ may be
        two bets a moment apart, so then each fill is tried on its own."""
        best, sure, exact = pick(self.closes.get(e.ticker, ()), e, self.used)
        if best is not None:
            self.used[(e.ticker, best[0])] = best[0]
            e.parts = []
            return [Match(best[4] or "", best[1], e, sure, e.ticker in self.tennis, best[0] - e.ts, not exact)]
        found = [m for part in e.parts for m in self._match(part)]
        if found:
            self.stats["split"] += 1
        return found

    def poll_trades(self, now: float | None = None) -> list[Match]:
        """Read the combo trades since the last poll and match them."""
        now = time.time() if now is None else now
        since = self._since if self._since is not None else now
        # Overlap the last poll a little: a trade can show up in the feed a moment after it happened.
        combos = [t for t in self.client.iter_trades(datetime.fromtimestamp(since - 5, timezone.utc))
                  if t["ticker"].startswith(COMBO_PREFIX)]
        new = [t for t in combos if t["trade_id"] not in self._seen]
        for t in new:
            self._seen[t["trade_id"]] = _ts(t["created_time"])
        self._since = now
        return self.match_trades(new, now)

    def reveal(self, now: float | None = None) -> None:
        """Look up the real ID behind hidden requesters whose RFQ filled."""
        now = time.time() if now is None else now
        for _ in range(min(self.reveal_per_poll, len(self._reveal))):
            item = self._reveal.popleft()
            m, tries, at = item
            if at > now:
                self._reveal.append(item)
                continue
            try:
                creator = self.client.get_rfq(m.rfq_id).get("creator_id") or PLACEHOLDER
            except Exception as exc:
                self.last_error = f"reveal: {exc!r}"[:200]
                creator = PLACEHOLDER
            if creator != PLACEHOLDER:
                m.creator = sys.intern(creator)
                self.stats["revealed"] += 1
                self.out.append(m)
            elif tries < 2:
                self._reveal.append([m, tries + 1, now + 60])
            else:
                self.stats["unrevealed"] += 1

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.keep
        with self.lock:
            for ticker in list(self.closes):
                closes = self.closes[ticker]
                if closes[-1][0] < cutoff:
                    del self.closes[ticker]
                elif closes[0][0] < cutoff:
                    self.closes[ticker] = [c for c in closes if c[0] >= cutoff]
            self.used = {k: t for k, t in self.used.items() if t >= cutoff}
            self.tennis = {k: t for k, t in self.tennis.items() if t >= now - 4 * self.keep}
        self._seen = {k: t for k, t in self._seen.items() if t >= now - 300}

    def run_matcher(self) -> None:
        while not self._stop.is_set():
            if not activity.active():
                activity.wait_active(self._stop)
                self._since = time.time()  # closes weren't heard while paused, so older trades can't match
            if self._stop.is_set():
                break
            try:
                self.poll_trades()
                self.reveal()
                self.prune()
            except Exception as exc:  # keep matching through API hiccups
                self.last_error = f"matcher: {exc!r}"[:200]
            self._stop.wait(self.poll_seconds)

    # ---- lifecycle and reads ----
    def start(self) -> "RfqBook":
        if not self._threads:
            self.started = time.time()
            self._since = self.started  # earlier trades can't match: their closes weren't heard
            for target, name in ((self.run_stream, THREAD_NAME), (self.run_matcher, MATCHER_NAME)):
                thread = threading.Thread(target=target, daemon=True, name=name)
                thread.stop = self.stop  # lets a reloaded app stop threads built from old code
                thread.start()
                self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stop.set()

    def take(self) -> list[Match]:
        """Matches made since the last call, with a known requester."""
        out = []
        while self.out:
            out.append(self.out.popleft())
        return out

    def held(self) -> int:
        """RFQ closes in memory now."""
        with self.lock:
            return sum(len(c) for c in self.closes.values())
