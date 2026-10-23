"""League and sport labels for combo legs, from series tickers seen in live combos (2026-10-23)."""

import pytest

from combo_tracker.sports import league_of, series_of


@pytest.mark.parametrize("ticker,expected", [
    ("KXNBAPTS-26OCT22CLEPHI-PHIJBROWN7", ("NBA", "basketball")),
    ("KXWNBAGAME-26JUN01LVNY-LV", ("WNBA", "basketball")),
    ("KXNCAAMBGAME-26MAR20DUKEUNC-DUKE", ("NCAAB", "basketball")),
    ("KXNCAABBGAME-26APR01LSUTAMU-LSU", ("NCAA BSB", "baseball")),  # college baseball, not basketball
    ("KXNFLRECYDSPROT-26OCT25CINBAL-X", ("NFL", "football")),
    ("KXNCAAFGAME-26OCT24JMUAPP-JMU", ("NCAAF", "football")),
    ("KXITFWMATCH-26OCT22ABCXYZ-ABC", ("ITF", "tennis")),
    ("KXATPCHALLENGERMATCH-26OCT22A-B", ("Challenger", "tennis")),
    ("KXEFLCHAMPIONSHIPGAME-26OCT25BIRCAR-BIR", ("Championship", "soccer")),
    ("KXLIGAEXPGAME-26OCT22ADOCAH-TIE", ("Liga Exp.", "soccer")),
    ("KXSAUDIPLBTTS-26OCT23A-B", ("Saudi PL", "soccer")),
    ("KXUFCMOV-26NOV01A-B", ("UFC", "mma")),
    ("KXETH15M-26OCT230300-T", ("ETH", "crypto")),
    ("KXGOLD15M-26OCT230300-T", ("Gold", "commodities")),
    ("KXPRESCUPMATCH-26SEP25-X", ("Pres. Cup", "golf")),
    ("SENATEKS-26-R", ("Senate", "politics")),
])
def test_league_of_known_series(ticker, expected):
    assert league_of(ticker) == expected


def test_unknown_series_falls_back_to_its_name():
    assert series_of("KXFOOBARGAME-26OCT22AB-A") == "KXFOOBARGAME"
    assert league_of("KXFOOBARGAME-26OCT22AB-A") == ("FOOBAR", "other")
