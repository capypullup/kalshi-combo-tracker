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
    from combo_tracker.sports import league_of
    from combo_tracker.view import pack

    bets = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 100), trade("2", "KXMVE-X", "no", 0.15, 100)])
    rows = [score_bet(b, combo_market(), {}, BOOKS, now=NOW) for b in bets]
    packed = pack(rows)
    assert len(packed["rows"]) == 2 and "id" not in packed["rows"][0]
    strs = packed["strs"]
    assert len(strs) == len(set(strs))  # each leg, event, league and sport name once, shared by both rows
    assert packed["rows"][0]["legs"][0]["n"] == packed["rows"][1]["legs"][0]["n"]
    leg = packed["rows"][1]["legs"][0]
    assert strs[leg["n"]] == rows[1]["leg_detail"][0]["leg"]
    assert (strs[leg["lg"]], strs[leg["spt"]]) == league_of(rows[1]["leg_detail"][0]["ticker"])


def test_schedule_decides_live_before_settlement_guess():
    from combo_tracker.pipeline import GameStarts
    from combo_tracker.view import is_live, leg_live

    class Schedule:
        def get_milestones(self, min_start, until, category="Sports", max_pages=20):
            return [{"start_date": "2026-10-21T11:30:00Z", "related_event_tickers": ["KXNFLGAME-26OCT21AB"]},
                    {"start_date": "2026-10-21T20:00:00Z", "primary_event_tickers": ["KXNBAGAME-26OCT21CD"]}]

    starts = GameStarts(Schedule())
    starts.refresh()
    assert starts("KXNFLGAME-26OCT21AB").hour == 11
    assert starts("KXNFLTOTAL-26OCT21AB").hour == 11  # same game, matched by the ticker's game part
    assert starts("KXNHLGAME-26OCT21EF") is None

    # Leg A's game started 30 min ago but settles in 5h (the old 3h rule said pregame).
    legs = {"A": {"status": "active", "expected_expiration_time": "2026-10-21T17:00:00Z"},
            "B": {"status": "active", "expected_expiration_time": "2026-10-21T13:00:00Z"}}
    bet = build_bets([trade("1", "KXMVE-X", "yes", 0.15, 10)])[0]
    row = score_bet(bet, combo_market(), legs, BOOKS, now=NOW, game_start=starts)
    a, b = row["leg_detail"]
    assert a["hours_to_start"] == pytest.approx(-0.5) and leg_live(a)
    assert b["hours_to_start"] == pytest.approx(-0.5)  # KXNFLTOTAL shares the game
    assert is_live(row)

    # A game that starts later is pregame even when its leg settles within 3h.
    market = combo_market()
    market["mve_selected_legs"] = [{"event_ticker": "KXNBAGAME-26OCT21CD", "market_ticker": "B", "side": "yes"}]
    row = score_bet(bet, market, legs, BOOKS, now=NOW, game_start=starts)
    assert not is_live(row)
    # With no schedule, fall back to the settlement-time guess.
    assert is_live(score_bet(bet, market, legs, BOOKS, now=NOW))


def test_scorer_keeps_the_newest_bets():
    from combo_tracker.demo import DemoClient
    from combo_tracker.pipeline import Scorer

    client = DemoClient()
    snap = Scorer(client).score(client.trades, min_stake=0, max_bets=20)
    newest = sorted(build_bets(client.trades), key=lambda b: b.last_time, reverse=True)[:20]
    assert {r["ticker"] for r in snap.rows} == {b.ticker for b in newest if b.ticker}
    assert sum(1 for r in snap.rows for l in r["leg_detail"] if l["start"] is not None) > 0  # demo schedule used


def test_check_flag_starts_where_the_pill_reads_plus_200():
    from combo_tracker.view import flags

    base = {"same_game_legs": 0, "missing_quotes": 0, "inactive_legs": 0, "outside_band": True}
    assert [flags({**base, "ev_pct_mid": x}) for x in (1.99, 1.996, 2.0, 5.0, None)] == ["", "CHECK", "CHECK", "CHECK", ""]
