"""Orderbook math for a single leg of a combo.

Kalshi books only list bids. Buying YES at the ask means matching a NO bid,
so the YES ask ladder is `1 - price` of each NO bid (and vice versa).
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _levels(raw: dict, side: str) -> list[tuple[float, float]]:
    """Bids for one side as [(price_dollars, size)], best (highest) first."""
    if f"{side}_dollars" in raw:
        levels = [(float(p), float(s)) for p, s in raw.get(f"{side}_dollars") or []]
    else:  # legacy integer-cents format
        levels = [(p / 100.0, float(s)) for p, s in raw.get(side) or []]
    return sorted((lv for lv in levels if lv[1] > 0), key=lambda lv: -lv[0])


@dataclass
class LegBook:
    """The book from the point of view of someone backing `side` of this leg."""

    ticker: str
    side: str  # "yes" or "no"
    bids: list[tuple[float, float]] = field(default_factory=list)  # best first
    asks: list[tuple[float, float]] = field(default_factory=list)  # best (lowest) first

    @classmethod
    def from_raw(cls, ticker: str, side: str, raw: dict) -> "LegBook":
        other = "no" if side == "yes" else "yes"
        bids = _levels(raw, side)
        asks = [(round(1.0 - p, 6), s) for p, s in _levels(raw, other)]
        return cls(ticker=ticker, side=side, bids=bids, asks=asks)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def top_ask_size(self) -> float:
        return self.asks[0][1] if self.asks else 0.0

    def ask_depth_within(self, cents: float) -> float:
        """Contracts available to buy within `cents` of the best ask."""
        if not self.asks:
            return 0.0
        limit = self.asks[0][0] + cents / 100.0 + 1e-9
        return sum(s for p, s in self.asks if p <= limit)

    def vwap_to_buy(self, qty: float) -> tuple[float | None, float]:
        """Average price to buy `qty` contracts by walking the asks.

        Returns (vwap, filled_qty). vwap is None if the book is empty;
        filled_qty < qty means the book ran out before the size was filled.
        """
        return _walk(self.asks, qty)

    def vwap_to_sell(self, qty: float) -> tuple[float | None, float]:
        """Average price to sell `qty` contracts by walking the bids."""
        return _walk(self.bids, qty)


def _walk(levels: list[tuple[float, float]], qty: float) -> tuple[float | None, float]:
    remaining, cost, filled = qty, 0.0, 0.0
    for price, size in levels:
        take = min(size, remaining)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return (cost / filled if filled else None), filled
