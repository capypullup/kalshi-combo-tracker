"""Turn raw combo trades into rows scored against the legs' orderbooks."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .orderbook import LegBook


def taker_fee(price: float, contracts: float, rate: float = 0.07) -> float:
    """Kalshi's standard taker fee: rate * C * P * (1 - P), rounded up to the cent."""
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100


def american_odds(p: float | None) -> int | None:
    """Probability or price (0-1) -> American odds, e.g. 0.8 -> -400, 0.2 -> +400."""
    if p is None or p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p > 0.5 else round(100 * (1 - p) / p)


def kelly_fraction(p: float | None, price: float) -> float | None:
    """Full-Kelly bankroll fraction for a contract costing `price` that pays $1 with probability p."""
    if p is None or not 0 < price < 1:
        return None
    return max(0.0, (p - price) / (1 - price))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _game_key(event_ticker: str) -> str:
    """'KXNFLSPREAD-26OCT15SEADEN' -> '26OCT15SEADEN', shared by every market on one game."""
    return event_ticker.split("-", 1)[1] if "-" in event_ticker else event_ticker


def _product(values: list[float | None]) -> float | None:
    if not values or any(v is None for v in values):
        return None
    return math.prod(values)


@dataclass
class ComboBet:
    """One combo position: a single fill, or all fills on one combo + side when grouped."""

    ticker: str
    taker_side: str  # "yes" = bet that every leg hits
    contracts: float
    price: float  # avg price per contract paid by the taker, in dollars
    first_time: datetime
    last_time: datetime
    fills: int
    trade_ids: list[str] = field(default_factory=list)

    @property
    def stake(self) -> float:
        return self.contracts * self.price


def _trade_side_price(trade: dict) -> tuple[str, float]:
    side = trade.get("taker_outcome_side") or trade["taker_side"]
    price = float(trade["yes_price_dollars"] if side == "yes" else trade["no_price_dollars"])
    return side, price


def build_bets(trades: list[dict], group: bool = True) -> list[ComboBet]:
    """Collapse raw trades into bets. With group=True, fills on the same combo and side merge."""
    bets: dict[tuple, ComboBet] = {}
    for t in trades:
        side, price = _trade_side_price(t)
        qty = float(t["count_fp"])
        ts = _parse_time(t["created_time"])
        key = (t["ticker"], side) if group else (t["trade_id"],)
        bet = bets.get(key)
        if bet is None:
            bets[key] = ComboBet(t["ticker"], side, qty, price, ts, ts, 1, [t["trade_id"]])
            continue
        total = bet.contracts + qty
        bet.price = (bet.price * bet.contracts + price * qty) / total
        bet.contracts = total
        bet.first_time = min(bet.first_time, ts)
        bet.last_time = max(bet.last_time, ts)
        bet.fills += 1
        bet.trade_ids.append(t["trade_id"])
    return list(bets.values())


def legs_of(market: dict) -> list[dict]:
    return market.get("mve_selected_legs") or []


def score_bet(bet: ComboBet, combo_market: dict, leg_markets: dict[str, dict], books: dict[str, dict],
              now: datetime | None = None, fee_rate: float = 0.07, events: dict[str, dict] | None = None) -> dict:
    """Score one bet. Fair prices are products of leg prices, i.e. legs are assumed independent."""
    now = now or datetime.now(timezone.utc)
    legs = legs_of(combo_market)
    leg_books = [LegBook.from_raw(l["market_ticker"], l["side"], books.get(l["market_ticker"], {})) for l in legs]

    p_mid = _product([b.mid for b in leg_books])
    p_ask = _product([b.best_ask for b in leg_books])
    p_bid = _product([b.best_bid for b in leg_books])
    # Fair value of the side the taker bought. The NO side wins when any leg misses,
    # so its "at the ask" counterpart is 1 - product of the leg bids. The at-size
    # price is the conservative (CRX) fair after walking the book for the bet's size:
    # leg bids for a YES bettor, leg asks for a NO bettor.
    if bet.taker_side == "yes":
        fills = [b.vwap_to_sell(bet.contracts) for b in leg_books]
        p_size = _product([vwap for vwap, _ in fills])
        fair_mid, fair_ask, fair_bid, fair_size = p_mid, p_ask, p_bid, p_size
    else:
        fills = [b.vwap_to_buy(bet.contracts) for b in leg_books]
        p_size = _product([vwap for vwap, _ in fills])
        flip = lambda p: None if p is None else 1 - p  # noqa: E731
        fair_mid, fair_ask, fair_bid, fair_size = flip(p_mid), flip(p_bid), flip(p_ask), flip(p_size)

    def edge(fair: float | None) -> float | None:
        return None if fair is None else fair - bet.price

    fee = taker_fee(bet.price, bet.contracts, fee_rate)
    e_mid, e_ask, e_bid, e_size = edge(fair_mid), edge(fair_ask), edge(fair_bid), edge(fair_size)
    ev_mid = None if e_mid is None else e_mid * bet.contracts
    ev_net = None if ev_mid is None else ev_mid - fee

    # Liquidity / thinness of the legs.
    spreads = [b.spread for b in leg_books]
    top_sizes = [b.top_ask_size for b in leg_books]
    fill_ratios = [filled / bet.contracts if bet.contracts else 1.0 for _, filled in fills]
    weakest = min(range(len(leg_books)), key=lambda i: (fill_ratios[i], top_sizes[i])) if leg_books else None
    missing_quotes = sum(1 for b in leg_books if b.mid is None)
    depth_ratios = [b.ask_depth_within(2) / bet.contracts if bet.contracts else None for b in leg_books]
    min_depth_ratio = min((d for d in depth_ratios if d is not None), default=None)
    inactive_legs = sum(1 for l in legs if leg_markets.get(l["market_ticker"], {}).get("status", "active") != "active")
    # Leg spreads compound, so a price inside [product of bids, product of asks] is noise.
    outside_band = None
    if fair_bid is not None and fair_ask is not None:
        lo, hi = sorted((fair_bid, fair_ask))
        outside_band = bet.price < lo or bet.price > hi

    game_keys = [_game_key(l["event_ticker"]) for l in legs]
    same_game_legs = len(game_keys) - len(set(game_keys))

    leg_expiries = [_parse_time(leg_markets.get(l["market_ticker"], {}).get("expected_expiration_time")) for l in legs]
    leg_expiries = [e for e in leg_expiries if e is not None]
    expiry = max(leg_expiries) if leg_expiries else _parse_time(
        combo_market.get("expected_expiration_time") or combo_market.get("close_time"))
    hours_to_resolve = None if expiry is None else (expiry - now).total_seconds() / 3600

    events = events or {}
    leg_rows = []
    for l, b, (vwap, filled) in zip(legs, leg_books, fills):
        lm = leg_markets.get(l["market_ticker"], {})
        leg_rows.append({
            "leg": lm.get("yes_sub_title") or lm.get("title") or l["market_ticker"],
            "event_title": events.get(l["event_ticker"], {}).get("title") or lm.get("title") or l["event_ticker"],
            "ticker": l["market_ticker"],
            "side": l["side"],
            "bid": b.best_bid,
            "ask": b.best_ask,
            "mid": b.mid,
            "spread": b.spread,
            "top_ask_size": b.top_ask_size,
            "depth_2c": b.ask_depth_within(2),
            "depth_ratio": b.ask_depth_within(2) / bet.contracts if bet.contracts else None,
            "status": lm.get("status"),
            "vwap_at_size": vwap,
            "fill_pct": filled / bet.contracts if bet.contracts else 1.0,
        })

    return {
        "ticker": bet.ticker,
        "title": combo_market.get("yes_sub_title") or combo_market.get("title") or bet.ticker,
        "side": bet.taker_side,
        "legs": len(legs),
        "contracts": bet.contracts,
        "price": bet.price,
        "stake": bet.stake,
        "max_payout": bet.contracts,
        "fills": bet.fills,
        "last_time": bet.last_time,
        "minutes_ago": (now - bet.last_time).total_seconds() / 60,
        "fair_mid": fair_mid,
        "fair_ask": fair_ask,
        "fair_bid": fair_bid,
        "fair_crx": fair_bid,
        "fair_at_size": fair_size,
        "edge_mid": e_mid,
        "edge_ask": e_ask,
        "edge_bid": e_bid,
        "edge_at_size": e_size,
        "roi_mid": None if e_mid is None or not bet.price else e_mid / bet.price,
        # EV % = fair / price - 1. CRX prices each leg by crossing the spread to the
        # opposite side (i.e. at its bid), the conservative counterpart to mid.
        "ev_pct_mid": None if fair_mid is None or not bet.price else fair_mid / bet.price - 1,
        "ev_pct_crx": None if fair_bid is None or not bet.price else fair_bid / bet.price - 1,
        "kelly_mid": kelly_fraction(fair_mid, bet.price),
        "kelly_crx": kelly_fraction(fair_bid, bet.price),
        "roi_net": None if ev_net is None or not bet.stake else ev_net / bet.stake,
        "ev_mid": ev_mid,
        "ev_ask": None if e_ask is None else e_ask * bet.contracts,
        "fee": fee,
        "ev_net": ev_net,
        "max_leg_spread": max((s for s in spreads if s is not None), default=None),
        "min_top_ask_size": min(top_sizes, default=None),
        "min_fill_pct": min(fill_ratios, default=None),
        "weakest_leg": leg_rows[weakest]["leg"] if weakest is not None else None,
        "min_depth_ratio": min_depth_ratio,
        "outside_band": outside_band,
        "missing_quotes": missing_quotes,
        "inactive_legs": inactive_legs,
        "same_game_legs": same_game_legs,
        "hours_to_resolve": hours_to_resolve,
        "leg_detail": leg_rows,
    }
