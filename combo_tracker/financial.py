"""Which legs are crypto, commodity or other financial-market markets, so combos with one can be hidden.

Kenny only bets sports, and combos with a 15-minute Bitcoin or gold leg crowd out the bets he wants.
Kalshi files each event under a category ("Crypto", "Commodities", ...), and every event the
scorer fetches teaches this module its series' category. Until a series has been seen, a list of
known financial series decides. That list matches whole series names (KXBTC15M, KXGOLD15M,
KXINXU), never prefixes, so sports series such as KXDOTA2GAME or KXGOLDCUPGAME stay visible.
"""

from __future__ import annotations

import re
import threading

# Kalshi event categories to hide. Economics (Fed, CPI) and Elections stay.
HIDDEN_CATEGORIES = {"Crypto", "Commodities", "Financials"}

# Known financial series, as seen in combo legs on 2026-10-23: crypto, metals and energy every
# 15 minutes (KXBTC15M ... KXWTI15M), daily/hourly crypto (KXBTCD), stock indices (KXINXU) and FX.
_ASSETS = ("BTC|ETH|SOL|XRP|DOGE|BNB|HYPE|ZEC|NEAR|ADA|AVAX|LTC|LINK|SHIBA|DOT|SUI|TRX|XLM|BCH|TON|PEPE"
           "|GOLD|SILVER|COPPER|PLATINUM|PALLADIUM|NATGAS|WTI|BRENT"
           "|INX|NASDAQ100|DJI|EURUSD|USDJPY|GBPUSD")
FINANCIAL_SERIES = re.compile(rf"KX(?:{_ASSETS})(?:15M|D|H|W|U|M|Y|MAXY|MINY)?")

_category: dict[str, str] = {}  # series ticker -> Kalshi category, learned from events
_lock = threading.Lock()


def series_of(ticker: str) -> str:
    """'KXBTC15M-26OCT230915-15' -> 'KXBTC15M'. Works for market and event tickers."""
    return ticker.split("-", 1)[0]


def learn(events: dict[str, dict]) -> None:
    """Remember each event's series category (events without one are skipped)."""
    found = {}
    for ticker, event in events.items():
        category = (event or {}).get("category")
        if category:
            found[(event.get("series_ticker") or series_of(ticker))] = category
    if found:
        with _lock:
            _category.update(found)


def is_financial(ticker: str) -> bool:
    """True for a leg (market or event ticker) on crypto, commodities or another financial market."""
    series = series_of(ticker)
    category = _category.get(series)
    if category is not None:
        return category in HIDDEN_CATEGORIES
    return FINANCIAL_SERIES.fullmatch(series) is not None


def has_financial_leg(legs: list[dict]) -> bool:
    """For a combo's mve_selected_legs."""
    return any(is_financial(l.get("market_ticker") or l.get("event_ticker") or "") for l in legs)
