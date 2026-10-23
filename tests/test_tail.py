from datetime import datetime, timezone

from combo_tracker.engine import build_bets
from combo_tracker.rfq import PLACEHOLDER, Execution, Match
from combo_tracker.tail import Tail, payouts_in, tickers_in, trades_of

T = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc).timestamp()


def match(creator, ticker="KXMVECOMBO-A", sec=0.0, tid="t1", side="yes", count=100.0, price=0.2):
    return Match("", creator, Execution(ticker, side, T + sec, count, price, [tid], T + sec), True)


def test_tickers_in_links_and_bare():
    assert tickers_in("https://kalshi.com/markets_by_ticker/KXMVECROSSCATEGORY-S2026ABC-1D2E3") == \
        ["KXMVECROSSCATEGORY-S2026ABC-1D2E3"]
    assert tickers_in("look kalshi.com/combos?marketTicker=kxmvesports-x1&foo=1 and KXMVESPORTS-X1.") == \
        ["KXMVESPORTS-X1"]
    assert tickers_in("no ticker here") == []


def test_payouts_in_positions_card_text():
    assert payouts_in("$1,999.76 pays $4,424.51") == [(1999.76, 4424.51)]
    assert payouts_in("2 MARKET COMBO\n$99.99 pays $171.16\nand $499.99 pays $1,133.52") == \
        [(99.99, 171.16), (499.99, 1133.52)]
    assert payouts_in("pays 50") == [(None, 50.0)]
    assert payouts_in("KXMVECOMBO-A") == []


def test_learn_from_one_bet_when_alone():
    tail = Tail("Sam")
    tail.add(match("friend"))
    tail.add(match("other", ticker="KXMVECOMBO-B"))
    tail.add(match(PLACEHOLDER, ticker="KXMVECOMBO-A", tid="t9"))  # hidden requesters aren't candidates
    reply = tail.learn_ticker("KXMVECOMBO-A")
    assert "Got it" in reply and tail.codes.keys() == {"friend"} and tail.candidates is None
    assert tail.is_friend(match("friend", ticker="KXMVECOMBO-Z"))
    assert not tail.is_friend(match("other"))
    assert "already theirs" in tail.learn_ticker("KXMVECOMBO-A")


def test_second_bet_narrows_candidates():
    tail = Tail()
    for who in ("friend", "x", "y"):
        tail.add(match(who, ticker="KXMVECOMBO-POPULAR", tid=who))
    tail.add(match("friend", ticker="KXMVECOMBO-B", tid="f2"))
    tail.add(match("y", ticker="KXMVECOMBO-B", tid="y2"))
    tail.add(match("q", ticker="KXMVECOMBO-B", tid="q2"))
    assert "3 people" in tail.learn_ticker("KXMVECOMBO-POPULAR") and not tail.codes
    assert "2 people" in tail.learn_ticker("KXMVECOMBO-B")
    assert tail.candidates == {"friend", "y"}
    tail.add(match("friend", ticker="KXMVECOMBO-C", tid="f3"))
    assert "Got it" in tail.learn_ticker("KXMVECOMBO-C") and set(tail.codes) == {"friend"}


def test_unknown_ticker_and_restart():
    tail = Tail()
    assert "haven't seen" in tail.learn_ticker("KXMVECOMBO-NONE")
    tail.add(match("a", ticker="KXMVECOMBO-1", tid="1"))
    tail.add(match("b", ticker="KXMVECOMBO-1", tid="2"))
    tail.add(match("c", ticker="KXMVECOMBO-2", tid="3"))
    tail.add(match("d", ticker="KXMVECOMBO-2", tid="4"))
    tail.learn_ticker("KXMVECOMBO-1")
    reply = tail.learn_ticker("KXMVECOMBO-2")  # nobody in common: start again from this combo
    assert "started over" in reply and tail.candidates == {"c", "d"}


def test_learn_by_payout_with_or_without_fee():
    tail = Tail("Sam")
    # $2,000 at 0.452 for 4,424.51 contracts; Kalshi's card may show the cost with the taker fee.
    tail.add(match("friend", ticker="KXMVECOMBO-BIG", tid="f", count=4424.51, price=0.452))
    tail.add(match("other", ticker="KXMVECOMBO-BIG", tid="o", count=4424.00, price=0.452))  # payout differs
    tail.add(match("small", ticker="KXMVECOMBO-S", tid="s", count=171.16, price=0.5842))
    stake = 4424.51 * 0.452
    reply = tail.learn_payout(round(stake, 2), 4424.51)
    assert "Got it" in reply and set(tail.codes) == {"friend"}
    tail = Tail()
    tail.add(match("friend", ticker="KXMVECOMBO-BIG", tid="f", count=4424.51, price=0.452))
    fee = 0.07 * 4424.51 * 0.452 * 0.548
    assert "Got it" in tail.learn_payout(round(stake + fee + 0.004, 2), 4424.51)
    tail = Tail()
    tail.add(match("friend", ticker="KXMVECOMBO-BIG", tid="f", count=4424.51, price=0.452))
    assert "haven't seen" in tail.learn_payout(500.00, 4424.51)  # cost doesn't fit
    assert "Got it" in tail.learn_payout(None, 4424.51)


def test_learn_by_payout_adds_up_fills_on_one_combo():
    tail = Tail()
    tail.add(match("friend", ticker="KXMVECOMBO-A", tid="1", count=100, price=0.3))
    tail.add(match("friend", ticker="KXMVECOMBO-A", tid="2", sec=60, count=50, price=0.36))
    tail.add(match("other", ticker="KXMVECOMBO-A", tid="3", count=150, price=0.3, side="no"))
    assert "Got it" in tail.learn_payout(48.00, 150) and set(tail.codes) == {"friend"}


def test_payout_and_ticker_narrow_together():
    tail = Tail()
    tail.add(match("friend", ticker="KXMVECOMBO-A", tid="1", count=100, price=0.3))
    tail.add(match("other", ticker="KXMVECOMBO-B", tid="2", count=100, price=0.3))
    assert "2 people" in tail.learn_payout(30.00, 100)
    tail.add(match("friend", ticker="KXMVECOMBO-C", tid="3"))
    tail.add(match("third", ticker="KXMVECOMBO-C", tid="4"))
    assert "Got it" in tail.learn_ticker("KXMVECOMBO-C") and set(tail.codes) == {"friend"}


def test_learn_from_social_feed_trade_ids():
    tail = Tail()
    tail.add(match("friend", tid="abc"))
    trades = [{"trade_id": "abc", "ticker": "KXMVECOMBO-A", "taker_nickname": "sam", "maker_nickname": "",
               "taker_side": "yes", "price_dollars": "0.2", "count_fp": "100", "create_date": "2026-10-23T01:00:00Z"},
              {"trade_id": "zzz", "ticker": "KXMVECOMBO-A", "taker_nickname": "someone", "maker_nickname": "",
               "taker_side": "yes", "price_dollars": "0.2", "count_fp": "5", "create_date": "2026-10-23T01:00:00Z"}]
    assert tail.learn_social(trades, "sam") == ["friend"]
    assert tail.learn_social(trades, "sam") == []  # already known


def test_prune_forgets_old_bets_and_state_round_trips():
    tail = Tail(keep_hours=1)
    tail.add(match("old", tid="o", sec=-7200))
    tail.add(match("new", ticker="KXMVECOMBO-B", tid="n"))
    tail.prune(now=T)
    assert set(tail.history) == {"KXMVECOMBO-B"} and set(tail.by_trade) == {"n"} and tail.held() == 1
    tail.adopt("new", "test")
    tail.sent["k"] = T
    again = Tail()
    again.load(tail.state())
    assert again.codes == {"new": "test"} and "k" in again.sent


def test_trades_of_prices_the_side_bought():
    yes = build_bets(trades_of(match("f", count=50, price=0.25)))[0]
    assert (yes.taker_side, yes.contracts, round(yes.price, 4)) == ("yes", 50, 0.25)
    no = build_bets(trades_of(match("f", side="no", count=10, price=0.9)))[0]
    assert (no.taker_side, round(no.price, 4)) == ("no", 0.9)


def test_messages():
    tail = Tail("Sam")
    plain = tail.plain_bet_message(match("f"), "42")
    assert plain["text"].startswith("👥 Sam bet $20 YES") and plain["chat_id"] == "42"
    assert "markets_by_ticker/KXMVECOMBO-A" in plain["reply_markup"]["inline_keyboard"][0][0]["url"]
    peek = tail.peek_message("KXMVECOMBO-A", "42")
    assert peek["disable_notification"] and "Not a bet yet" in peek["text"]
