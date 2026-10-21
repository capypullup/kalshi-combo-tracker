import pytest

from combo_tracker.engine import american_odds, kelly_fraction
from combo_tracker.format import bet_card


def test_american_odds():
    assert american_odds(0.8) == -400
    assert american_odds(0.2) == 400
    assert american_odds(0.5) == 100
    assert american_odds(100 / 4679) == 4579
    assert american_odds(None) is None and american_odds(1.0) is None


def test_kelly_fraction():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0.0


def test_card_matches_requested_format():
    """Kenny's example: +4579 combo, fair -383 at mid and -363 at CRX, quarter Kelly, 1u = 1%."""
    price, mid, crx = 100 / 4679, 383 / 483, 363 / 463
    row = {
        "side": "yes", "price": price,
        "fair_mid": mid, "fair_crx": crx,
        "ev_pct_mid": mid / price - 1, "ev_pct_crx": crx / price - 1,
        "kelly_mid": kelly_fraction(mid, price), "kelly_crx": kelly_fraction(crx, price),
        "leg_detail": [
            {"side": "no", "leg": "Mookie Betts: 2+", "event_title": "Milwaukee vs Los Angeles D: Home Runs"},
            {"side": "no", "leg": "Freddy Peralta: 6+", "event_title": "Cleveland vs Tampa Bay: Strikeouts"},
        ],
    }
    assert bet_card(row) == (
        "YES Combo +4579\n"
        "────────────\n"
        "NO Mookie Betts: 2+ (Milwaukee vs Los Angeles D: Home Runs)\n"
        "NO Freddy Peralta: 6+ (Cleveland vs Tampa Bay: Strikeouts)\n"
        "────────────\n"
        "EV MID +3610.3% (fair -383) · 20u\n"
        "EV CRX +3568.4% (fair -363) · 19u"
    )
