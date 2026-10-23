"""Faster filters (batched event titles, pre-scored Min bets, one build per window) and hiding
crypto / commodity combos."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from combo_tracker import alerts, financial, pipeline
from combo_tracker.client import KalshiClient
from combo_tracker.collector import Collector
from combo_tracker.pipeline import Board, Scorer


@pytest.fixture(autouse=True)
def fresh_categories(monkeypatch):
    monkeypatch.setattr(financial, "_category", {})


# ---- which legs are financial ----
@pytest.mark.parametrize("ticker", [
    "KXBTC15M-26OCT230915-15", "KXETH15M-26OCT230915-15", "KXZEC15M-X", "KXNEAR15M-X", "KXGOLD15M-26OCT230915-15",
    "KXSILVER15M-X", "KXNATGAS15M-X", "KXWTI15M-X", "KXCOPPER15M-X", "KXBTCD-26OCT2317-T108000", "KXINXU-26OCT23H1600",
    "KXEURUSDH-X", "KXDOT15M-X"])
def test_financial_series_are_hidden(ticker):
    assert financial.is_financial(ticker)


@pytest.mark.parametrize("ticker", [
    "KXDOTA2GAME-26OCT23LIQTS-LIQ", "KXGOLDCUPGAME-X", "KXSOLHEIMCUP-X", "KXATPMATCH-26OCT23SINALC-SIN",
    "KXNBAPTS-26OCT23ATLCHA-ATLTYOUNG11-25", "CONTROLS-2026-D", "KXNFLGAME-26OCT26SEADEN-SEA"])
def test_sports_and_elections_stay(ticker):
    assert not financial.is_financial(ticker)


def test_kalshi_category_beats_the_series_list():
    financial.learn({"KXNEWCOIN-26OCT23": {"category": "Crypto", "series_ticker": "KXNEWCOIN"},
                     "KXGOLD15M-26OCT23": {"category": "Sports", "series_ticker": "KXGOLD15M"},
                     "KXNOCAT-1": {"title": "no category"}})
    assert financial.is_financial("KXNEWCOIN-26OCT23-T5")
    assert not financial.is_financial("KXGOLD15M-26OCT23-T5")  # learned beats the list
    assert not financial.is_financial("KXNOCAT-1-A")


def test_alerts_skip_gold_but_not_dota():
    def row(leg):
        return {"leg_detail": [{"ticker": "KXNBAGAME-26OCT23ATLCHA-ATL"}, {"ticker": leg}]}
    assert alerts.skipped(row("KXGOLD15M-26OCT230915-15"))
    assert not alerts.skipped(row("KXDOTA2GAME-26OCT23LIQTS-LIQ"))  # the old KXDOT prefix hid Dota 2
    assert not alerts.skipped(row("KXGOLD15M-26OCT230915-15"), ())


# ---- a fake Kalshi for the scorer ----
NOW = datetime.now(timezone.utc)


def trade(i, ticker, stake_contracts=100, minutes_ago=1.0, price=0.25):
    t = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {"trade_id": f"t{i}", "ticker": ticker, "taker_side": "yes", "taker_outcome_side": "yes",
            "yes_price_dollars": f"{price:.4f}", "no_price_dollars": f"{1 - price:.4f}",
            "count_fp": f"{stake_contracts:.2f}", "created_time": t}


class Kalshi:
    """Combos KXMVE-<n>; even ones have a sports leg pair, the ones in `crypto` add a BTC 15M leg."""

    def __init__(self, crypto=(), categories=None):
        self.crypto = set(crypto)
        self.categories = categories or {}
        self.calls = {"markets": 0, "events": 0}

    def legs(self, combo):
        n = int(combo.split("-")[1])
        legs = [{"event_ticker": f"KXNBAGAME-26OCT23G{n}", "market_ticker": f"KXNBAGAME-26OCT23G{n}-A", "side": "yes"}]
        if n in self.crypto:
            legs.append({"event_ticker": "KXBTC15M-26OCT230915", "market_ticker": "KXBTC15M-26OCT230915-15", "side": "yes"})
        else:
            legs.append({"event_ticker": f"KXNEWX-{n}", "market_ticker": f"KXNEWX-{n}-A", "side": "yes"})
        return legs

    def get_markets(self, tickers, batch_size=100):
        self.calls["markets"] += 1
        out = {}
        for t in tickers:
            if t.startswith("KXMVE"):
                out[t] = {"ticker": t, "mve_selected_legs": self.legs(t)}
            else:
                out[t] = {"ticker": t, "yes_bid_dollars": "0.49", "yes_ask_dollars": "0.51",
                          "yes_bid_size_fp": "500", "yes_ask_size_fp": "500", "status": "active"}
        return out

    def get_events(self, tickers):
        self.calls["events"] += 1
        return {t: {"event_ticker": t, "title": t, "series_ticker": t.split("-")[0],
                    "category": self.categories.get(t.split("-")[0], "Sports")} for t in tickers}


def test_hidden_combos_dont_shrink_the_list():
    kalshi = Kalshi(crypto={1, 2, 3})
    trades = [trade(i, f"KXMVE-{i}", minutes_ago=i / 10) for i in range(1, 11)]  # KXMVE-1 is the newest
    snap = Scorer(kalshi, hide=financial.is_financial).score(trades, max_bets=5)
    assert [r["ticker"] for r in snap.rows] == ["KXMVE-4", "KXMVE-5", "KXMVE-6", "KXMVE-7", "KXMVE-8"]
    assert snap.bets_over_min == 7  # 10 bets, 3 hidden
    assert len(Scorer(kalshi).score(trades, max_bets=5).rows) == 5  # without hide (the tail bot) nothing is dropped


def test_a_new_series_found_to_be_crypto_is_dropped_from_its_first_list():
    kalshi = Kalshi(categories={"KXNEWX": "Crypto"})  # not on the built-in list, so only its events tell
    trades = [trade(1, "KXMVE-4"), trade(2, "KXMVE-6", minutes_ago=2)]
    scorer = Scorer(kalshi, hide=financial.is_financial)
    snap = scorer.score(trades, max_bets=5)
    assert snap.rows == [] and snap.bets_over_min == 0
    assert financial.is_financial("KXNEWX-9-A")


def test_event_title_failure_still_scores():
    class Flaky(Kalshi):
        def get_events(self, tickers):
            raise requests.ConnectionError("reset")

    snap = Scorer(Flaky()).score([trade(1, "KXMVE-4")], max_bets=5)
    assert len(snap.rows) == 1 and any("event titles" in e for e in snap.errors)


# ---- batched event titles ----
class Resp:
    def __init__(self, status, data):
        self.status_code, self.data, self.headers = status, data, {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def client_with(handler):
    c = KalshiClient(rate_per_sec=1000)
    c.session.get = lambda url, params=None, timeout=None, headers=None: handler(url, params or {})
    return c


def test_events_come_100_per_call():
    seen = []

    def handler(url, params):
        seen.append(params)
        asked = params["tickers"].split(",")
        return Resp(200, {"events": [{"event_ticker": t, "title": t} for t in asked if t != "GONE-1"]})

    c = client_with(handler)
    out = c.get_events([f"E-{i}" for i in range(250)] + ["GONE-1"])
    assert len(seen) == 3 and len(out) == 250 and "GONE-1" not in out  # missing ones aren't cached as {}
    assert c.batch_events_ok


def test_events_fall_back_to_one_call_each_if_the_list_is_ignored():
    def handler(url, params):
        if url.endswith("/events"):
            return Resp(200, {"events": [{"event_ticker": "SOMETHING-ELSE"}]})
        ticker = url.rsplit("/", 1)[1]
        return Resp(200, {"event": {"event_ticker": ticker, "title": ticker}})

    c = client_with(handler)
    assert set(c.get_events(["A-1", "B-2"])) == {"A-1", "B-2"} and c.batch_events_ok is False


def test_events_rate_limited_come_back_empty_and_are_retried(monkeypatch):
    monkeypatch.setattr("combo_tracker.client.time.sleep", lambda s: None)
    c = client_with(lambda url, params: Resp(503, {}))
    assert c.get_events(["A-1"]) == {} and c.batch_events_ok is None  # nothing learned from an outage


# ---- the board ----
class CountingScorer(Scorer):
    def __init__(self, client):
        super().__init__(client)
        self.views = []

    def score_bets(self, bets, n_trades, min_stake=0.0, max_bets=400, quote_age=None):
        self.views.append(min_stake)
        return super().score_bets(bets, n_trades, min_stake, max_bets, quote_age)


def test_new_views_are_scored_on_the_next_poll_and_each_window_is_built_once(monkeypatch):
    built = []
    real = pipeline.build_bets
    monkeypatch.setattr(pipeline, "build_bets", lambda trades, group=True: built.append(len(trades)) or real(trades, group))
    trades = [trade(i, f"KXMVE-{i}", stake_contracts=40 * i) for i in range(1, 30)]
    board = Board(CountingScorer(Kalshi()), trades_for=lambda m: trades, every=120, keep=((60, 50.0),))
    board.refresh()
    assert board.peek(60, 100) is not None and board.peek(60, 50) is not None and len(built) == 1
    for m in (5, 10, 500):
        board.touch(60, m)
    board.refresh()  # well before `every`: only the views without scores yet
    assert all(board.peek(60, m) is not None for m in (5, 10, 500)) and len(built) == 2
    assert sorted(board.scorer.views) == [5, 10, 50, 100, 500]


def test_page_waits_for_the_score_already_running():
    gate, started = threading.Event(), threading.Event()

    class Slow(Scorer):
        calls = 0

        def score_bets(self, *args, **kwargs):
            Slow.calls += 1
            started.set()
            gate.wait(5)
            return super().score_bets(*args, **kwargs)

    board = Board(Slow(Kalshi()), trades_for=lambda m: [trade(1, "KXMVE-4")], every=120)
    worker = threading.Thread(target=board.refresh)
    worker.start()
    started.wait(5)
    got = []
    page = threading.Thread(target=lambda: got.append(board.get(60, 100)))
    page.start()
    time.sleep(0.1)
    gate.set()
    page.join(5)
    worker.join(5)
    assert got and got[0] is board.peek(60, 100) and Slow.calls == 1


def test_tap_served_early_is_answered_again_by_the_next_poll(monkeypatch):
    board = Board(CountingScorer(Kalshi()), trades_for=lambda m: [trade(1, "KXMVE-4")], every=120, stale_request=30)
    board.refresh()
    since = board.request(60, 100)
    clock = time.time()
    monkeypatch.setattr(pipeline.time, "time", lambda: clock + 40)  # the poll after the tap is slow
    board.refresh(trades_as_of=since - 5)
    assert board.done(60, 100, since) == (False, None) and (60, 100.0) in board.requests
    board.refresh(trades_as_of=since + 1)
    assert board.done(60, 100, since) == (True, None) and not board.requests


# ---- the collector ----
def test_refresh_tap_cuts_backfill_short_without_a_gap():
    class Feed:
        def __init__(self, col):
            self.col = col

        def iter_trades(self, min_ts, max_ts=None, max_pages=0):
            for i in range(5000):  # newest first, one a second back from max_ts
                ts = (max_ts or NOW) - timedelta(seconds=i)
                if ts < min_ts:
                    return
                if i == 2500:
                    self.col.poll_now()  # Kenny taps refresh mid-chunk
                yield {"trade_id": f"x{ts.timestamp()}", "ticker": "KXNBAGAME-X", "created_time": ts.isoformat()}

    col = Collector(None, max_window_minutes=180)
    col.client = Feed(col)
    col.covered_to = col.covered_from = NOW
    col.extend_backfill()
    assert NOW - timedelta(seconds=2500) <= col.covered_from < NOW  # where the scan got to, not the chunk start
