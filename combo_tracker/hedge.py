"""Hedge calculator: how much to bet on the other side to lock in a profit (or cover your stake).

Prices are what one dollar of payout costs (0-1), the same as a Kalshi contract price, so a
bet of S at price p returns S / p if it wins. Odds can be typed as Kalshi cents, American odds
or a multiplier; parse_odds turns any of them into a price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .engine import taker_fee

MODES = ("Lock profit", "Cover stake", "Custom")


def parse_odds(text: str) -> float | None:
    """Turn typed odds into a price (0-1), or None if it can't be read.

    "+450" / "-120" / "450"  American odds (any number 100 or more is American)
    "4.5x" / "4.5"          multiplier, the total return per $1 (decimal odds; a plain
                            number with a decimal point from 1 up)
    "22¢" / "22c" / "22"    Kalshi cents (a plain whole number from 1 to 99)
    "0.22"                  a price in dollars
    """
    t = (text or "").strip().lower().replace(" ", "").replace(",", "")
    m = re.fullmatch(r"([+-]?)(\d+(?:\.\d+)?)(x|¢|c)?", t)
    if not m:
        return None
    sign, n, unit = m.group(1), float(m.group(2)), m.group(3)
    if unit == "x":
        price = 1 / n if n > 1 else None
    elif unit in ("¢", "c"):
        price = n / 100 if not sign else None
    elif sign or n >= 100:
        if n < 100:
            return None
        price = 100 / (n + 100) if sign != "-" else n / (n + 100)
    elif n < 1:
        price = n
    elif "." in m.group(2):
        price = 1 / n if n > 1 else None
    else:
        price = n / 100
    return price if price is not None and 0 < price < 1 else None


@dataclass
class Hedge:
    stake: float  # what the original bet cost
    price: float  # its price
    hedge_price: float
    hedge: float  # dollars to bet on the other side, before its fee
    fee: float  # the hedge's Kalshi taker fee (0 if not on Kalshi)

    @property
    def payout(self) -> float:
        return self.stake / self.price

    @property
    def hedge_payout(self) -> float:
        return self.hedge / self.hedge_price

    @property
    def contracts(self) -> float:
        return self.hedge_payout

    @property
    def if_original_wins(self) -> float:
        return self.payout - self.stake - self.hedge - self.fee

    @property
    def if_hedge_wins(self) -> float:
        return self.hedge_payout - self.hedge - self.fee - self.stake

    @property
    def worst(self) -> float:
        return min(self.if_original_wins, self.if_hedge_wins)


def hedge(stake: float, price: float, hedge_price: float, mode: str = "Lock profit",
          amount: float | None = None, kalshi_fee: bool = True) -> Hedge:
    """Size the hedge.

    Lock profit: the same result whichever side wins (on Kalshi: as many contracts as you hold).
    Cover stake: if the original bet loses, the hedge pays back its stake, so you can't lose money.
    Custom: hedge `amount` dollars.
    """
    if not (stake > 0 and 0 < price < 1 and 0 < hedge_price < 1):
        raise ValueError("stake must be positive and prices between 0 and 1")

    def fee_for(h: float) -> float:
        return taker_fee(hedge_price, h / hedge_price) if kalshi_fee and h > 0 else 0.0

    if mode == "Lock profit":
        h = stake / price * hedge_price  # equal payouts; the fee is paid in both outcomes
    elif mode == "Cover stake":
        h = stake * hedge_price / (1 - hedge_price)
        for _ in range(5):  # the fee grows with the hedge, so solve h/q - h - fee(h) = stake
            h = (stake + fee_for(h)) * hedge_price / (1 - hedge_price)
    elif mode == "Custom":
        h = max(0.0, amount or 0.0)
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return Hedge(stake, price, hedge_price, h, fee_for(h))
