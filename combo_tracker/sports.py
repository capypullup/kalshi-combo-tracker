"""Which league and sport a Kalshi market belongs to, from its series ticker.

A market ticker starts with its series, e.g. "KXNBAPTS-26OCT22CLEPHI-PHIJBROWN7" is in series
KXNBAPTS (NBA player points). Series names are a league code plus a market type, so the
longest matching prefix below decides. The table was checked against the series behind live
combo legs (2026-10-23); anything new falls back to a cleaned-up series name.
"""

from __future__ import annotations

import re

# Prefix of the series ticker (after "KX") -> (league label, sport). Longest prefix wins.
LEAGUES: dict[str, tuple[str, str]] = {
    # American football
    "NFL": ("NFL", "football"), "NCAAF": ("NCAAF", "football"), "CFB": ("NCAAF", "football"),
    "UFL": ("UFL", "football"), "CFL": ("CFL", "football"),
    # Basketball
    "NBA": ("NBA", "basketball"), "WNBA": ("WNBA", "basketball"), "NCAAMB": ("NCAAB", "basketball"),
    "NCAAB": ("NCAAB", "basketball"), "NCAAWB": ("NCAAW", "basketball"), "EUROLEAGUE": ("EuroLeague", "basketball"),
    "UNRIVALED": ("Unrivaled", "basketball"), "LNBP": ("LNBP", "basketball"), "BBL": ("BBL", "basketball"),
    "MARMAD": ("NCAAB", "basketball"),
    # Baseball
    "MLB": ("MLB", "baseball"), "NPB": ("NPB", "baseball"), "KBO": ("KBO", "baseball"),
    "NCAABB": ("NCAA BSB", "baseball"), "NCAABASEBALL": ("NCAA BSB", "baseball"),
    # Hockey
    "NHL": ("NHL", "hockey"), "KHL": ("KHL", "hockey"),
    # Tennis
    "ATP": ("ATP", "tennis"), "WTA": ("WTA", "tennis"), "ITF": ("ITF", "tennis"),
    "ATPCHALLENGER": ("Challenger", "tennis"), "CHALLENGER": ("Challenger", "tennis"),
    "DAVISCUP": ("Davis Cup", "tennis"), "BJKCUP": ("BJK Cup", "tennis"), "TENNIS": ("Tennis", "tennis"),
    # Soccer
    "EPL": ("EPL", "soccer"), "PREMIERLEAGUE": ("EPL", "soccer"), "LALIGA": ("La Liga", "soccer"),
    "SERIEA": ("Serie A", "soccer"), "BUNDESLIGA": ("Bundesliga", "soccer"), "LIGUE1": ("Ligue 1", "soccer"),
    "UCL": ("UCL", "soccer"), "UEL": ("Europa", "soccer"), "UECL": ("Conference", "soccer"),
    "MLS": ("MLS", "soccer"), "NWSL": ("NWSL", "soccer"), "LIGAMX": ("Liga MX", "soccer"),
    "LIGAEXP": ("Liga Exp.", "soccer"), "EFL": ("EFL", "soccer"), "EFLCHAMP": ("Championship", "soccer"),
    "EREDIVISIE": ("Eredivisie", "soccer"), "SPL": ("Scottish", "soccer"), "SAUDIPL": ("Saudi PL", "soccer"),
    "BRASILEIRAO": ("Brasileirão", "soccer"), "LIGAPORTUGAL": ("Portugal", "soccer"), "FACUP": ("FA Cup", "soccer"),
    "COPPAITALIA": ("Coppa Italia", "soccer"), "COPADELREY": ("Copa del Rey", "soccer"), "DFBPOKAL": ("DFB Pokal", "soccer"),
    "WORLDCUP": ("World Cup", "soccer"), "FIFA": ("FIFA", "soccer"), "SOCCER": ("Soccer", "soccer"),
    "DIMAYOR": ("Colombia", "soccer"), "ALEAGUE": ("A-League", "soccer"), "BRASIL": ("Brasileirão", "soccer"),
    "CLUBWC": ("Club WC", "soccer"), "CONMEBOL": ("CONMEBOL", "soccer"), "CONCACAF": ("CONCACAF", "soccer"),
    "UEFANL": ("Nations Lg", "soccer"), "COUPEDEFRANCE": ("Coupe de Fr.", "soccer"), "KLEAGUE": ("K League", "soccer"),
    "JLEAGUE": ("J League", "soccer"), "SCOTTISH": ("Scottish", "soccer"), "SERIEB": ("Serie B", "soccer"),
    "ARGPREMIER": ("Argentina", "soccer"), "SUPERLIG": ("Süper Lig", "soccer"), "SWISSLEAGUE": ("Swiss", "soccer"),
    "EKSTRAKLASA": ("Ekstraklasa", "soccer"), "VLEAGUE": ("V League", "soccer"), "USLCUP": ("USL", "soccer"),
    # Combat sports
    "UFC": ("UFC", "mma"), "MMA": ("MMA", "mma"), "PFL": ("PFL", "mma"), "BOXING": ("Boxing", "boxing"),
    # Golf
    "PGA": ("PGA", "golf"), "LPGA": ("LPGA", "golf"), "LIV": ("LIV", "golf"), "DPWORLD": ("DP World", "golf"),
    "GOLF": ("Golf", "golf"), "PRESCUP": ("Pres. Cup", "golf"), "RYDERCUP": ("Ryder Cup", "golf"),
    # Motor racing
    "F1": ("F1", "racing"), "NASCAR": ("NASCAR", "racing"), "INDYCAR": ("IndyCar", "racing"), "MOTOGP": ("MotoGP", "racing"),
    # Esports
    "CS2": ("CS2", "esports"), "CSGO": ("CS2", "esports"), "LOL": ("LoL", "esports"), "VALORANT": ("Valorant", "esports"),
    "DOTA": ("Dota 2", "esports"), "COD": ("CoD", "esports"), "R6": ("R6", "esports"),
    # Cricket
    "CRICKET": ("Cricket", "cricket"), "IPL": ("IPL", "cricket"), "T20": ("T20", "cricket"),
    # Crypto and markets
    "BTC": ("BTC", "crypto"), "ETH": ("ETH", "crypto"), "SOL": ("SOL", "crypto"), "XRP": ("XRP", "crypto"),
    "DOGE": ("DOGE", "crypto"), "HYPE": ("HYPE", "crypto"), "NEAR": ("NEAR", "crypto"), "BNB": ("BNB", "crypto"),
    "ZEC": ("ZEC", "crypto"), "ADA": ("ADA", "crypto"), "AVAX": ("AVAX", "crypto"), "LTC": ("LTC", "crypto"),
    "LINK": ("LINK", "crypto"), "SHIBA": ("SHIB", "crypto"),
    "GOLD": ("Gold", "commodities"), "SILVER": ("Silver", "commodities"), "PLATINUM": ("Platinum", "commodities"),
    "PALLADIUM": ("Palladium", "commodities"), "COPPER": ("Copper", "commodities"), "WTI": ("Oil", "commodities"),
    "NATGAS": ("Nat gas", "commodities"),
    "INX": ("S&P 500", "finance"), "NASDAQ": ("Nasdaq", "finance"), "DJI": ("Dow", "finance"),
    # Politics
    "SENATE": ("Senate", "politics"), "GOV": ("Governor", "politics"),
    "PRES": ("President", "politics"), "GOVPARTY": ("Governor", "politics"),
}
_PREFIXES = sorted(LEAGUES, key=len, reverse=True)
# Market-type words that end a series name, dropped when a series isn't in the table.
_TAIL = re.compile(r"(GAME|MATCH|SPREAD|TOTAL|BTTS|WINNER|DOUBLES|FIGHT|RACE|PTS|PROPS?|15M|D)$")


def series_of(ticker: str) -> str:
    """'KXNBAPTS-26OCT22CLEPHI-PHIJBROWN7' -> 'KXNBAPTS'."""
    return ticker.split("-", 1)[0].upper()


def league_of(ticker: str) -> tuple[str, str]:
    """(league, sport) for a market, event or series ticker, e.g. ("NBA", "basketball").

    Unknown series give their own name without the KX prefix and market-type suffix, and sport "other".
    """
    code = series_of(ticker)
    code = code[2:] if code.startswith("KX") else code
    for prefix in _PREFIXES:
        if code.startswith(prefix):
            return LEAGUES[prefix]
    name = _TAIL.sub("", code) or code
    return (name[:10], "other")
