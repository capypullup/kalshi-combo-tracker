"""Links that open a market in Kalshi's app, or on its website when the app isn't installed."""

from urllib.parse import quote

KALSHI = "https://kalshi.com"


def bet_url(ticker: str) -> str:
    """The link that puts this market in Kalshi's bet slip.

    Kalshi's iOS and Android apps claim kalshi.com/markets_by_ticker/* links. On the website a
    combo's link opens the combo builder with that combo in the slip (/combos?marketTicker=…) and
    a single market's opens its order panel. ui/table.js builds the same link (tests check it).
    """
    return f"{KALSHI}/markets_by_ticker/{quote(ticker, safe='')}"
