"""Links that put a market in a Kalshi bet slip."""

from urllib.parse import quote

from .client import COMBO_PREFIX

KALSHI = "https://kalshi.com"


def bet_url(ticker: str) -> str:
    """The link that puts this market in a Kalshi bet slip.

    A combo's link opens kalshi.com's combo builder with that combo in the slip
    (/combos?marketTicker=…, the link Kalshi's own shared combos use). It stays in the browser
    on purpose: the Kalshi app can't load a combo from a link. iOS hands /markets_by_ticker/*
    links to the app, which opened without the combo, and the app doesn't claim /combos.
    A single market's link opens its page in the app, or its order panel on the web.
    ui/table.js and ui/home.js build the same links (tests check it).
    """
    t = quote(ticker, safe="")
    if ticker.upper().startswith(COMBO_PREFIX):
        return f"{KALSHI}/combos?marketTicker={t}"
    return f"{KALSHI}/markets_by_ticker/{t}"
