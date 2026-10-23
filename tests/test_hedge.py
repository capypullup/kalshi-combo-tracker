import pytest

from combo_tracker.hedge import hedge, parse_odds


@pytest.mark.parametrize("text,price", [
    ("+300", 0.25), ("300", 0.25), ("-300", 0.75), ("+100", 0.5), ("4x", 0.25), ("1.25x", 0.8),
    ("22¢", 0.22), ("22c", 0.22), ("22", 0.22), ("0.22", 0.22), ("2.5", 0.4), (" +4,579 ", 100 / 4679),
])
def test_parse_odds(text, price):
    assert parse_odds(text) == pytest.approx(price)


@pytest.mark.parametrize("text", ["", "abc", "+50", "0", "1x", "0.5x", "100¢", "-22¢", "1.0"])
def test_parse_odds_rejects(text):
    assert parse_odds(text) is None


def test_lock_profit_is_equal_both_ways():
    # $100 at +400 (20¢), hedge at 60¢ (-150): 500 contracts' worth, hedge $300 -> +$100 either way
    h = hedge(100, 0.20, 0.60, kalshi_fee=False)
    assert h.hedge == pytest.approx(300)
    assert h.if_original_wins == pytest.approx(100)
    assert h.if_hedge_wins == pytest.approx(100)


def test_lock_profit_on_kalshi_buys_matching_contracts_and_pays_fee():
    h = hedge(100, 0.20, 0.60)
    assert h.contracts == pytest.approx(500)
    assert h.fee == pytest.approx(8.40)  # 0.07 * 500 * 0.6 * 0.4
    assert h.if_original_wins == pytest.approx(h.if_hedge_wins) == pytest.approx(91.60)


def test_lock_can_be_a_loss_when_the_prices_add_past_a_dollar():
    h = hedge(100, 0.50, 0.60, kalshi_fee=False)
    assert h.worst == pytest.approx(-20)


def test_cover_stake_breaks_even_if_original_loses():
    h = hedge(100, 0.20, 0.60, mode="Cover stake")
    assert h.if_hedge_wins == pytest.approx(0, abs=0.01)
    assert h.if_original_wins > 0
    nofee = hedge(100, 0.20, 0.60, mode="Cover stake", kalshi_fee=False)
    assert nofee.hedge == pytest.approx(150) and nofee.if_original_wins == pytest.approx(250)


def test_custom_amount():
    h = hedge(100, 0.20, 0.60, mode="Custom", amount=60, kalshi_fee=False)
    assert h.if_original_wins == pytest.approx(340)
    assert h.if_hedge_wins == pytest.approx(-60)
    assert hedge(100, 0.2, 0.6, mode="Custom", amount=0).fee == 0


def test_bad_inputs():
    with pytest.raises(ValueError):
        hedge(0, 0.2, 0.6)
    with pytest.raises(ValueError):
        hedge(100, 0.2, 1.0)
