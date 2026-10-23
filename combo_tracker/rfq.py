"""Put a stable ID on combo bettors by matching RFQs to executed combo trades.

Every combo bet on Kalshi starts as an RFQ (request for quote). Kalshi broadcasts
each RFQ to every logged-in user with the requester's `creator_id`: a salted hash
that stays the same for that user across RFQs. The public trade feed has the
executed combo trades, but no trader ID. An RFQ and the trade it became share:

- the combo market ticker,
- the size (the RFQ asks for N contracts, or for $X of cost), and
- the timing (the trade prints a few seconds after the RFQ was created, and the
  RFQ closes right after).

So for each RFQ we look for combo trades on the same market, of the same size,
inside its time window. When exactly one RFQ fits a trade and that trade is the
only fit for the RFQ, the match is "sure"; when several fit, the closest in time
wins and the match is "likely". The requester accepts a maker's quote, so the
requester is the taker: their side is the trade's taker side.

Requesters can choose to hide their ID (everyone then sees one shared placeholder
hash), but Kalshi reveals the real ID once the RFQ executes, so we look those up
again after matching.

This needs a Kalshi API key: the RFQ feed isn't public.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .engine import taker_fee

# sha256("") — what everyone sees as creator_id while a requester hides theirs.
PLACEHOLDER = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
THREAD_NAME = "rfq-stream"


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


@dataclass
class Rfq:
    id: str
    creator: str
    ticker: str
    created: float
    contracts: float | None = None
    target: float | None = None  # dollars, including fees unless target_excludes_fees
    target_excludes_fees: bool = False
    closed: float | None = None
    legs: list[dict] = field(default_factory=list)

    @property
    def hidden(self) -> bool:
        return self.creator == PLACEHOLDER

    @classmethod
    def from_api(cls, d: dict) -> "Rfq | None":
        created = _ts(d.get("created_ts"))
        if not d.get("id") or not d.get("market_ticker") or created is None:
            return None
        contracts = _float(d.get("contracts_fp") or d.get("contracts"))
        return cls(
            id=d["id"], creator=d.get("creator_id") or PLACEHOLDER, ticker=d["market_ticker"], created=created,
            contracts=contracts if contracts else None, target=_float(d.get("target_cost_dollars")) or None,
            target_excludes_fees=bool(d.get("target_cost_excludes_fees")),
            closed=_ts(d.get("cancelled_ts") or d.get("deleted_ts")) or (
                _ts(d.get("updated_ts")) if d.get("status") == "closed" else None),
            legs=d.get("mve_selected_legs") or [],
        )


@dataclass
class Execution:
    """One or more combo fills on a market, same side, within a second or so."""

    ticker: str
    side: str
    ts: float
    count: float
    price: float  # avg price per contract of `side`
    trade_ids: list[str]


@dataclass
class Match:
    rfq_id: str
    creator: str
    execution: Execution
    sure: bool  # the only fit both ways; otherwise the closest in time among several


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
                out.append(Execution(ticker, side, _ts(group[0]["created_time"]), count, price,
                                     [g["trade_id"] for g in group]))
                group = []
            if t is not None:
                group.append(t)
    return out


def size_fits(rfq: Rfq, ex: Execution, cost_tol: float = 0.03) -> bool:
    """Does this execution have the size the RFQ asked for?"""
    if rfq.contracts:
        return abs(ex.count - rfq.contracts) <= max(0.011, 0.001 * rfq.contracts)
    if rfq.target:
        principal = ex.count * ex.price
        cost = principal if rfq.target_excludes_fees else principal + taker_fee(ex.price, ex.count)
        # Contracts come in 0.01 steps, so the cost can miss the target by about a price tick.
        return abs(cost - rfq.target) <= max(0.05, cost_tol * rfq.target)
    return False


def match(rfqs: list[Rfq], execs: list[Execution], before: float = 2.0, after_close: float = 15.0,
          max_open: float = 120.0) -> list[Match]:
    """Pair RFQs with the executions they became. Each side is used at most once."""
    by_ticker: dict[str, list[Execution]] = defaultdict(list)
    for e in execs:
        by_ticker[e.ticker].append(e)
    pairs = []
    fits_per_exec: dict[int, int] = defaultdict(int)
    fits_per_rfq: dict[str, int] = defaultdict(int)
    for r in rfqs:
        end = (r.closed if r.closed is not None else r.created + max_open) + after_close
        for e in by_ticker.get(r.ticker, ()):
            if r.created - before <= e.ts <= end and size_fits(r, e):
                anchor = r.closed if r.closed is not None else r.created
                pairs.append((abs(e.ts - anchor), r, e))
                fits_per_exec[id(e)] += 1
                fits_per_rfq[r.id] += 1
    pairs.sort(key=lambda x: x[0])
    used_r, used_e, out = set(), set(), []
    for _, r, e in pairs:
        if r.id in used_r or id(e) in used_e:
            continue
        used_r.add(r.id)
        used_e.add(id(e))
        out.append(Match(r.id, r.creator, e, sure=fits_per_exec[id(e)] == 1 and fits_per_rfq[r.id] == 1))
    return out


class RfqBook:
    """Keeps recent RFQs from Kalshi's websocket (and REST polls) and matches them to
    combo trades from the collector.

    `matches` grows for as long as the app runs: trade_id -> Match.
    """

    def __init__(self, client, combo_trades, keep_minutes: int = 180, reveal_per_cycle: int = 50):
        self.client = client
        self.combo_trades = combo_trades  # minutes -> combo trades in the v2 feed shape
        self.keep = keep_minutes * 60
        self.reveal_per_cycle = reveal_per_cycle
        self.lock = threading.Lock()
        self.rfqs: dict[str, Rfq] = {}
        self.matches: dict[str, Match] = {}  # trade_id -> match
        self.matched_rfqs: set[str] = set()
        self.revealed: dict[str, str] = {}  # rfq id -> real creator id, for hidden requesters
        self.stats = defaultdict(int)
        self.ws_status = "not started"
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- intake ----
    def ingest(self, d: dict) -> None:
        r = Rfq.from_api(d)
        if r is None:
            return
        with self.lock:
            old = self.rfqs.get(r.id)
            if old is not None:
                # REST and the stream both report the same RFQ; keep what each knows.
                r.closed = r.closed or old.closed
                r.contracts = r.contracts or old.contracts
                r.target = r.target or old.target
                if r.creator == PLACEHOLDER:
                    r.creator = old.creator
            else:
                self.stats["rfqs"] += 1
            self.rfqs[r.id] = r

    def close(self, rfq_id: str, when: float | None) -> None:
        with self.lock:
            r = self.rfqs.get(rfq_id)
            if r is not None and r.closed is None:
                r.closed = when or time.time()

    def on_message(self, raw: str) -> None:
        msg = json.loads(raw)
        kind, body = msg.get("type"), msg.get("msg") or {}
        if kind == "rfq_created":
            self.ingest(body)
        elif kind == "rfq_deleted":
            self.close(body.get("id"), _ts(body.get("deleted_ts")))
        elif kind == "error":
            self.last_error = f"stream: {body}"[:200]

    def poll_rest(self, max_pages: int = 20) -> None:
        """Catch RFQs the stream missed (e.g. while reconnecting)."""
        cursor = None
        cutoff = time.time() - self.keep
        for _ in range(max_pages):
            data = self.client.get_rfqs(cursor=cursor)
            rows = data.get("rfqs") or []
            for d in rows:
                self.ingest(d)
            cursor = data.get("cursor")
            oldest = min((_ts(d.get("created_ts")) or 0 for d in rows), default=0)
            if not cursor or not rows or oldest < cutoff:
                break

    # ---- websocket ----
    def run_stream(self) -> None:
        import websocket  # websocket-client

        backoff = 1.0
        while not self._stop.is_set():
            try:
                headers = self.client.auth.headers("GET", "/trade-api/ws/v2")
                ws = websocket.create_connection(WS_URL, header=[f"{k}: {v}" for k, v in headers.items()], timeout=30)
                ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["communications"]}}))
                self.ws_status = "connected"
                backoff = 1.0
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping()
                        continue
                    if raw:
                        self.on_message(raw)
                ws.close()
            except Exception as exc:  # reconnect after network errors
                self.ws_status = "reconnecting"
                self.last_error = f"stream: {exc!r}"[:200]
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def start(self) -> "RfqBook":
        if self._thread is None:
            self._thread = threading.Thread(target=self.run_stream, daemon=True, name=THREAD_NAME)
            self._thread.stop = self.stop
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # ---- matching ----
    def match_now(self) -> list[Match]:
        """Match RFQs to the collector's combo trades; returns the new matches."""
        now = time.time()
        with self.lock:
            self.rfqs = {k: r for k, r in self.rfqs.items() if r.created >= now - self.keep}
            open_rfqs = [r for k, r in self.rfqs.items() if k not in self.matched_rfqs]
        trades = [t for t in self.combo_trades(self.keep // 60) if t["trade_id"] not in self.matches]
        new = match(open_rfqs, executions(trades))
        for m in new:
            self.matched_rfqs.add(m.rfq_id)
            for tid in m.execution.trade_ids:
                self.matches[tid] = m
            self.stats["sure" if m.sure else "likely"] += 1
        self.reveal()
        return new

    def reveal(self) -> None:
        """Look up the real ID behind hidden requesters whose RFQ executed."""
        todo = [m for m in {id(m): m for m in self.matches.values()}.values()
                if m.creator == PLACEHOLDER and m.rfq_id not in self.revealed][: self.reveal_per_cycle]
        for m in todo:
            try:
                creator = self.client.get_rfq(m.rfq_id).get("creator_id") or PLACEHOLDER
            except Exception as exc:
                self.last_error = f"reveal: {exc!r}"[:200]
                break
            self.revealed[m.rfq_id] = creator
            if creator != PLACEHOLDER:
                m.creator = creator
                self.stats["revealed"] += 1

    def by_creator(self) -> dict[str, list[Match]]:
        out: dict[str, list[Match]] = defaultdict(list)
        for m in {id(m): m for m in self.matches.values()}.values():
            if m.creator != PLACEHOLDER:
                out[m.creator].append(m)
        return dict(out)
