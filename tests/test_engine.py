from datetime import datetime, timezone

import pytest

from combo_tracker.engine import build_bets, score_bet, taker_fee
from combo_tracker.orderbook import LegBook

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def book(yes, no):
    return {"yes_dollars": [[f"{p:.4f}", f"{s:.2f}"] for p, s in yes],
            "no_dollars": [[f"{p:.4f}", f"{s:.2f}"] for p, s in no]}


def trade(tid, ticker, side, yes_price, count, t="2026-10-21T11:50:00Z"):
    return {"trade_id": tid, "ticker": ticker, "taker_side": side, "taker_outcome_side": side,
            "yes_price_dollars": f"{yes_price:.4f}", "no_price_dollars": f"{1 - yes_price:.4f}",
            "count_fp": f"{count:.2f}", "created_time": t}


def test_legbook_yes_side_uses_no_bids_as_asks():
    b = LegBook.from_raw("A", "yes", book(yes=[(0.58, 10), (0.60, 5)], no=[(0.38, 7), (0.35, 20)]))
    assert b.best_bid == 0.60
    assert b.best_ask == pytest.approx(0.62)
    assert b.mid == pytest.approx(0.61)
    assert b.spread == pytest.approx(0.02)
    vwap, filled = b.vwap_to_buy(10)
    assert filled == 10
    assert vwap == pytest.approx((7 * 0.62 + 3 * 0.65) / 10)


def test_legbook_no_side_and_thin_book():
    b = LegBook.from_raw("A", "no", book(yes=[(0.60, 5)], no=[(0.38, 7)]))
    assert b.best_bid == 0.38 and b.best_ask == pytest.approx(0.40)
    vwap, filled = b.vwap_to_buy(50)
    assert filled == 5  # book ran out
    assert LegBook.from_raw("A", "yes", {}).mid is None


def test_legacy_cents_format():
    b = LegBook.from_raw("A", "yes", {"yes": [[60, 5]], "no": [[38, 7]]})
    assert b.best_ask == pytest.approx(0.62)


def test_taker_fee_rounds_up():
    assert taker_fee(0.5, 100) == 1.75
    assert taker_fee(0.5, 1) == 0.02


def test_build_bets_groups_fills():
    trades = [trade("1", "KXMVE-X", "yes", 0.20, 10), trade("2", "KXMVE-X", "yes", 0.30, 30),
              trade("3", "KXMVE-X", "no", 0.20, 5)]
    bets = build_bets(trades, group=True)
    yes = next(b for b in bets if b.taker_side == "yes")
    assert yes.contracts == 40 and yes.fills == 2
    assert yes.price == pytest.approx(0.275)
    no = next(b for b in bets if b.taker_side == "no")
    assert no.price == pytest.approx(0.80)
    assert len(build_bets(trades, group=False)) == 3


def combo_market():
    return {"ticker": "KXMVE-X", "yes_sub_title": "yes A,yes B",
            "expected_expiration_time": "2026-10-21T18:00:00Z",
            "mve_selected_legs": [
                {"event_ticker": "KXNFLGAME-26OCT21AB", "market_ticker": "A", "side": "yes"},
                {"event_ticker": "KXNFLTOTAL-26OCT21AB", "market_ticker": "B", "side": "yes"}]}


BOOKS = {"A": book(yes=[(0.49, 100)], no=[(0.49, 100)]),   # mid .50, ask .51
         "B": book(yes=[(0.39, 100)], no=[(0.59, 100)])}   # mid .40, ask .41


def test_score_yes_bet():
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 100)])[0]
    row = score_bet(bet, combo_market(), {}, BOOKS, now=NOW)
    assert row["fair_mid"] == pytest.approx(0.20)
    assert row["fair_ask"] == pytest.approx(0.51 * 0.41)
    assert row["edge_mid"] == pytest.approx(0.05)
    assert row["ev_mid"] == pytest.approx(5.0)
    assert row["ev_net"] == pytest.approx(5.0 - taker_fee(0.15, 100))
    assert row["same_game_legs"] == 1
    assert row["hours_to_resolve"] == pytest.approx(6.0)
    assert row["min_fill_pct"] == 1.0


def test_score_no_bet_mirrors_yes():
    bet = build_bets([trade("1", "KXMVE-X", "no", 0.15, 10)])[0]  # pays 0.85 for NO
    row = score_bet(bet, combo_market(), {}, BOOKS, now=NOW)
    assert row["fair_mid"] == pytest.approx(0.80)
    assert row["edge_mid"] == pytest.approx(-0.05)
    assert row["fair_ask"] == pytest.approx(1 - 0.49 * 0.39)


def test_missing_leg_quote_gives_none():
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 10)])[0]
    row = score_bet(bet, combo_market(), {}, {"A": BOOKS["A"]}, now=NOW)
    assert row["fair_mid"] is None and row["ev_mid"] is None
    assert row["missing_quotes"] == 1


def test_band_depth_and_roi():
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 100)])[0]
    row = score_bet(bet, combo_market(), {}, BOOKS, now=NOW)
    # band is 0.49*0.39=0.191 .. 0.51*0.41=0.209; 0.15 is below it
    assert row["outside_band"] is True
    assert row["min_depth_ratio"] == pytest.approx(1.0)
    assert row["roi_net"] == pytest.approx(row["ev_net"] / 15.0)
    inside = build_bets([trade("2", "KXMVE-X", "yes", 0.20, 10)])[0]
    assert score_bet(inside, combo_market(), {}, BOOKS, now=NOW)["outside_band"] is False


def test_leg_expiry_and_status():
    legs = {"A": {"status": "active", "expected_expiration_time": "2026-10-21T15:00:00Z"},
            "B": {"status": "closed", "expected_expiration_time": "2026-10-22T12:00:00Z"}}
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 10)])[0]
    row = score_bet(bet, combo_market(), legs, BOOKS, now=NOW)
    assert row["hours_to_resolve"] == pytest.approx(24.0)
    assert row["hours_to_first_leg"] == pytest.approx(3.0)
    assert row["inactive_legs"] == 1


def test_at_size_is_conservative_and_flags_thin_books():
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 100)])[0]
    row = score_bet(bet, combo_market(), {}, BOOKS, now=NOW)
    assert row["fair_at_size"] == pytest.approx(0.49 * 0.39)  # walks the leg bids
    big = build_bets([trade("2", "KXMVE-X", "yes", 0.15, 400)])[0]
    assert score_bet(big, combo_market(), {}, BOOKS, now=NOW)["min_fill_pct"] == pytest.approx(0.25)


def test_slim_row_is_json_safe_and_flags_live_games():
    import json

    from combo_tracker.view import slim

    legs = {"A": {"status": "active", "expected_expiration_time": "2026-10-21T14:00:00Z"},
            "B": {"status": "active", "expected_expiration_time": "2026-10-22T12:00:00Z"}}
    market = combo_market()
    market["mve_selected_legs"][1]["event_ticker"] = "KXNFLTOTAL-26OCT21CD"  # second game
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 100)])[0]
    row = slim(score_bet(bet, market, legs, {"A": BOOKS["A"]}, now=NOW))  # leg B has no quote
    json.dumps(row, allow_nan=False)
    assert row["id"] == "KXMVE-X|yes" and row["L"] == 2 and row["G"] == 2 and row["sg"] == 0
    assert row["live"] is True  # leg A settles within 3h, so its game has started
    assert row["evm"] is None and "NO QUOTE" in row["flags"]
    assert [l["s"] for l in row["legs"]] == ["yes", "yes"]


def test_pack_sends_each_name_once():
    from combo_tracker.view import pack

    bets = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 100), trade("2", "KXMVE-X", "no", 0.15, 100)])
    rows = [score_bet(b, combo_market(), {}, BOOKS, now=NOW) for b in bets]
    packed = pack(rows)
    assert len(packed["rows"]) == 2 and "id" not in packed["rows"][0]
    assert len(packed["strs"]) == 4  # two leg names + two event names, shared by both rows
    leg = packed["rows"][1]["legs"][0]
    assert packed["strs"][leg["n"]] == rows[1]["leg_detail"][0]["leg"]
