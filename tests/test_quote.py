from datetime import datetime, timedelta, timezone

import pytest

from combo_tracker.demo import DemoClient
from combo_tracker.quote import DemoRfqDesk, Fill, check, quote_for, verdict

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


def fill(sec_ago, price, side="yes", n=100):
    return Fill(NOW - timedelta(seconds=sec_ago), side, price, n)


def test_resting_order_at_price_is_live():
    word, _, best = verdict("yes", 0.013, 1079, 0.013, [], NOW)
    assert word == "LIVE" and best == 0.013


def test_fresh_fill_at_price_is_live():
    word, reason, best = verdict("yes", 0.20, 0, None, [fill(10, 0.20), fill(40, 0.21)], NOW)
    assert word == "LIVE" and "10s ago" in reason and best == 0.20


def test_older_fill_at_price_is_likely():
    assert verdict("yes", 0.20, 0, None, [fill(300, 0.20)], NOW)[0] == "LIKELY"


def test_newer_fill_at_worse_price_is_moved():
    assert verdict("yes", 0.20, 0, None, [fill(20, 0.25), fill(200, 0.20)], NOW)[0] == "MOVED"


def test_other_side_fills_dont_count():
    assert verdict("yes", 0.20, 0, None, [fill(5, 0.80, side="no")], NOW)[0] == "NO DATA"
    assert verdict("yes", 0.20, 0, None, [fill(900, 0.20)], NOW)[0] == "STALE"


def test_quote_prices_from_the_requesters_side():
    raw = {"id": "q1", "creator_id": "1a8d9999", "contracts_fp": "89.54", "yes_bid_dollars": "0.4400",
           "no_bid_dollars": "0.4590", "status": "open"}
    yes = quote_for(raw, "yes")
    assert yes.price == pytest.approx(0.541) and yes.maker == "1a8d"
    assert yes.all_in > yes.price  # taker fee per contract on top
    assert quote_for(raw, "no").price == pytest.approx(0.56)
    assert quote_for({**raw, "no_bid_dollars": "0"}, "yes") is None


def test_check_scores_a_demo_combo():
    client = DemoClient()
    t = client.trades[0]
    c = check(client, t["ticker"], t["taker_outcome_side"], float(t["yes_price_dollars"]), 100)
    assert c.verdict in ("LIVE", "LIKELY", "MOVED", "STALE", "NO DATA")
    assert c.scored["leg_detail"] and c.fills


def test_demo_desk_answers_and_cancels(monkeypatch):
    desk = DemoRfqDesk({"T": 0.3})
    rid = desk.create("T", 50)
    import combo_tracker.quote as q
    monkeypatch.setattr(q.time, "time", lambda: desk.open[rid][2] + 10)
    quotes = [quote_for(r, "yes") for r in desk.quotes(rid)]
    assert len(quotes) == 3 and all(qt.price > 0.3 for qt in quotes)
    desk.cancel(rid)
    assert desk.quotes(rid) == []


def test_the_bets_own_fill_doesnt_make_it_live():
    client = DemoClient()
    t = client.trades[0]
    ts = datetime.fromisoformat(t["created_time"])
    newest = max(datetime.fromisoformat(x["created_time"]) for x in client.trades if x["ticker"] == t["ticker"])
    side = t["taker_outcome_side"]
    c = check(client, t["ticker"], side, 0.99 if side == "yes" else 0.99, 100, now=newest + timedelta(seconds=5),
              since=newest)
    assert c.verdict == "STALE" and c.fills  # the fills are still listed
