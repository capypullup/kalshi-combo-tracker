import math

import pytest

from combo_tracker.bettors import (BettorTracker, Fill, Profile, combo_fair_yes, is_tennis, mid_at, score_fill,
                                   summarize, to_fill)
from combo_tracker.demo import DemoClient
from combo_tracker.view import pack_bettors


def trade(**kw):
    base = {"trade_id": "t1", "ticker": "KXATPMATCH-26OCT21BONPAU-PAU", "price_dollars": "0.3000", "count_fp": "100.00",
            "create_date": "2026-10-21T20:00:30Z", "taker_side": "yes", "taker_action": "buy",
            "taker_nickname": "sharp", "maker_nickname": "mm"}
    return {**base, **kw}


def test_to_fill_taker_and_maker_sides():
    taker = to_fill(trade(), "sharp")
    assert (taker.side, taker.price, taker.count, taker.taker) == ("yes", 0.3, 100.0, True)
    maker = to_fill(trade(), "mm")
    assert (maker.side, maker.taker) == ("no", False)
    assert maker.price == pytest.approx(0.7)
    assert to_fill(trade(taker_side="no"), "sharp").price == pytest.approx(0.7)
    assert to_fill(trade(), "someone.else") is None


def candle(end, bid_open, ask_open, bid_close=None, ask_close=None):
    return {"end_period_ts": end,
            "yes_bid": {"open_dollars": f"{bid_open}", "close_dollars": f"{bid_close if bid_close is not None else bid_open}"},
            "yes_ask": {"open_dollars": f"{ask_open}", "close_dollars": f"{ask_close if ask_close is not None else ask_open}"}}


def test_mid_at_uses_book_before_the_trade():
    candles = [candle(1000, 0.40, 0.42, 0.44, 0.46), candle(1180, 0.50, 0.52)]
    assert mid_at(candles, 1150) == pytest.approx(0.51)  # inside the 1120-1180 minute: its open
    assert mid_at(candles, 1100) == pytest.approx(0.45)  # gap after 1000: last close
    assert mid_at(candles, 900) is None  # nothing before
    assert mid_at([candle(1000, 0.0, 0.42)], 990) is None  # one-sided book


def test_combo_fair_is_product_of_leg_sides():
    legs = [{"market_ticker": "A", "side": "yes"}, {"market_ticker": "B", "side": "no"}]
    assert combo_fair_yes(legs, {"A": 0.5, "B": 0.2}) == pytest.approx(0.4)
    assert combo_fair_yes(legs, {"A": 0.5, "B": None}) is None


def test_score_fill_ev_pnl_and_fee():
    f = Fill("t", 0, "X", "yes", 0.30, 100, True)
    s = score_fill(f, 0.40, "yes")
    fee = math.ceil(0.07 * 100 * 0.3 * 0.7 * 100) / 100
    assert s["fee"] == pytest.approx(fee)
    assert s["ev"] == pytest.approx(10 - fee)
    assert s["pnl"] == pytest.approx(70 - fee)
    maker = Fill("m", 0, "X", "no", 0.70, 100, False)
    s = score_fill(maker, 0.40, "yes")
    assert s["fee"] == 0 and s["ev"] == pytest.approx(-10) and s["pnl"] == pytest.approx(-70)
    assert score_fill(f, None, None)["ev"] is None


def test_summarize_compares_pnl_with_ev_on_same_trades():
    fills = [Fill(f"t{i}", i, "X", "yes", 0.5, 10, False) for i in range(4)]
    fair = {"t0": 0.5, "t1": 0.5, "t2": 0.5, "t3": None}
    s = summarize(fills, fair, {"X": "yes"})
    assert s["n"] == 4 and s["settled"] == 4 and s["ev_cover"] == 0.75
    assert s["pnl"] == pytest.approx(20)  # 4 × (1 - 0.5) × 10
    assert s["edge"] == pytest.approx(15)  # t3 has no EV, so it's left out of PnL − EV
    assert s["z"] == pytest.approx(15 / math.sqrt(3 * 100 * 0.25))


def test_is_tennis():
    assert is_tennis("KXWTAMATCH-26OCT21JOVPAO-PAO")
    assert not is_tennis("KXNBAGAME-26OCT21SACLAC-LAC")
    assert is_tennis("KXMVECROSSCATEGORY-X-Y", [{"event_ticker": "KXITFMATCH-26OCT21AB"}])


def test_tracker_cycle_on_demo_data():
    tracker = BettorTracker(DemoClient(), every=1)
    tracker.cycle()
    snap = tracker.snapshot
    assert not snap.errors
    rows = {r["nick"]: r for r in snap.rows}
    visible = [r for r in snap.rows if r["hidden"] is False]
    assert visible and all(r["all"]["n"] > 0 for r in visible)
    assert all(r["all"]["ev_cover"] == 1 for r in visible)  # the demo has candles for every market
    # Makers are filled at their own bid, so the book says their trades were worth something.
    assert rows["JohnR0809"]["all"]["ev"] > 0
    packed = pack_bettors(snap.rows)
    assert packed[0]["tennis"]["n"] >= 0 and "recent" in packed[0]

    tracker.add("@ace.pilot")
    tracker.add("Blackbriar")
    tracker.serve_wanted()
    rows = {r["nick"]: r for r in tracker.snapshot.rows}
    assert rows["ace.pilot"]["src"] == ["feed", "you"]
    assert rows["Blackbriar"]["hidden"] is True and rows["Blackbriar"]["all"]["n"] == 0


def test_history_stops_at_known_trades():
    client = DemoClient()
    tracker = BettorTracker(client, every=1)
    p = tracker.profiles.setdefault("Hughman", Profile("Hughman"))
    tracker.refresh_history(p, force=True)
    first = len(p.fills)
    calls = []
    real = client.social_trades
    client.social_trades = lambda *a, **k: calls.append(k) or real(*a, **k)
    tracker.refresh_history(p, force=True)
    assert len(p.fills) == first and len(calls) == 1


def test_use_key_starts_one_rfq_book_across_sessions_and_switches_keys():
    import threading
    import time
    from types import SimpleNamespace

    started = []

    class Book:
        def __init__(self, auth):
            self.client = SimpleNamespace(auth=auth)

        def start(self):
            time.sleep(0.01)  # a real start spins up threads, so another session's run gets in
            started.append(self)
            return self

    tracker = BettorTracker(DemoClient())
    old, new = object(), object()
    runs = [threading.Thread(target=tracker.use_key, args=(old, Book)) for _ in range(8)]
    for t in runs:
        t.start()
    for t in runs:
        t.join()
    assert len(started) == 1 and tracker.rfq is started[0] and tracker.rfq.client.auth is old
    tracker.use_key(new, Book)
    assert len(started) == 1 and tracker.rfq.client.auth is new
