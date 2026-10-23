import time
from datetime import datetime, timedelta, timezone

from combo_tracker.collector import Collector
from combo_tracker.pipeline import TTLCache


class FakeClient:
    def __init__(self):
        self.trades = []  # newest first, like the API
        self.market_calls = 0

    def add(self, tid, ticker, minutes_ago):
        t = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        self.trades.append({"trade_id": tid, "ticker": ticker, "created_time": t, "taker_side": "yes",
                            "yes_price_dollars": "0.2500", "no_price_dollars": "0.7500", "count_fp": "10.00"})
        self.trades.sort(key=lambda x: x["created_time"], reverse=True)

    def iter_trades(self, min_ts, max_ts=None, max_pages=0):
        for t in self.trades:
            ts = datetime.fromisoformat(t["created_time"])
            if ts >= min_ts and (max_ts is None or ts <= max_ts):
                yield t

    def get_markets(self, tickers):
        self.market_calls += 1
        return {t: {"ticker": t, "mve_selected_legs": []} for t in tickers}


def test_collector_keeps_only_combos_and_polls_incrementally():
    c = FakeClient()
    c.add("1", "KXMVECROSS-A", 30)
    c.add("2", "KXNBAGAME-X", 20)
    c.add("3", "KXMVECROSS-B", 200)  # older than the initial backfill
    col = Collector(c, max_window_minutes=180, initial_backfill_minutes=60)
    col.poll()
    assert set(col.trades) == {"1"}
    c.add("4", "KXMVECROSS-C", 0)
    col.poll()
    assert set(col.trades) == {"1", "4"}
    assert [t["trade_id"] for t in col.window(10)] == ["4"]
    assert 59 <= col.coverage_minutes() <= 61
    t = col.window(10)[0]
    assert t["yes_price_dollars"] == 0.25 and t["count_fp"] == 10.0 and t["taker_outcome_side"] == "yes"


def test_backfill_extends_to_full_window():
    c = FakeClient()
    c.add("old", "KXMVECROSS-A", 150)
    col = Collector(c, max_window_minutes=180, initial_backfill_minutes=60)
    col.poll()
    assert "old" not in col.trades
    for _ in range(5):
        col.extend_backfill()
    assert "old" in col.trades
    assert col.coverage_minutes() >= 179


def test_ttl_cache_fetches_only_missing():
    calls = []
    cache = TTLCache(None)

    def fetch(keys):
        calls.append(list(keys))
        return {k: {"v": k} for k in keys}

    cache.get_many(["a", "b"], fetch)
    out = cache.get_many(["a", "b", "c"], fetch)
    assert calls == [["a", "b"], ["c"]]
    assert set(out) == {"a", "b", "c"}
    cache.data["a"] = (time.time() - 20, {"v": "a"})
    cache.get_many(["a", "b"], fetch, max_age=10)  # a refresh wants quotes from the last 10 s
    assert calls[-1] == ["a"]


def test_stop_ends_the_collector_thread():
    class Idle:
        def iter_trades(self, *args, **kwargs):
            return iter(())

    col = Collector(Idle(), poll_seconds=30, initial_backfill_minutes=0).start()
    assert col._thread.is_alive()
    col._thread.stop()
    col._thread.join(timeout=2)
    assert not col._thread.is_alive()


def test_board_scores_opened_views_in_the_background():
    from combo_tracker.pipeline import Board

    class FakeScorer:
        def __init__(self):
            self.calls = []

        def score(self, trades, min_stake, max_bets, quote_age=None):
            self.calls.append(min_stake)
            return f"snap {min_stake} #{len(self.calls)}"

    scorer = FakeScorer()
    board = Board(scorer, trades_for=lambda minutes: [], every=120, default_view=(60, 100.0))
    assert board.peek(60, 100) is None
    assert board.get(15, 50) == "snap 50.0 #1"  # first look is scored right away
    assert board.get(15, 50) == "snap 50.0 #1"  # then served from memory
    board.refresh()
    assert sorted(scorer.calls) == [50.0, 50.0, 100.0]  # default view and the opened one
    board.refresh()
    assert len(scorer.calls) == 3  # not again until `every` seconds pass
    board.views[(15, 50.0)] -= 3600  # nobody has looked for an hour
    board.refresh(force=True)
    assert board.peek(15, 50) is None and board.peek(60, 100) is not None


class FakeScorer:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def score(self, trades, min_stake, max_bets, quote_age=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.calls.append((min_stake, quote_age) if quote_age else min_stake)
        return f"snap {min_stake} #{len(self.calls)}"


def test_refresh_tap_is_answered_from_a_poll_after_the_tap():
    from combo_tracker.pipeline import Board

    board = Board(FakeScorer(), trades_for=lambda minutes: [], every=120, default_view=(60, 100.0))
    board.refresh()
    since = board.request(60, 100)
    board.refresh(trades_as_of=since - 5)  # this poll began before the tap
    assert board.done(60, 100, since) == (False, None)
    board.refresh(trades_as_of=since + 1)
    assert board.done(60, 100, since) == (True, None)
    assert board.peek(60, 100) == "snap 100.0 #2" and not board.requests


def test_failed_refresh_reports_its_error():
    from combo_tracker.pipeline import Board

    board = Board(FakeScorer(fail="Kalshi said 503"), trades_for=lambda minutes: [], every=120)
    since = board.request(60, 100)
    board.refresh(trades_as_of=since + 1)
    done, error = board.done(60, 100, since)
    assert done and "503" in error


def test_regular_pass_steps_aside_for_a_refresh_tap():
    from combo_tracker.pipeline import Board

    scorer = FakeScorer()
    board = Board(scorer, trades_for=lambda minutes: [], every=120, default_view=(60, 100.0))
    board.get(15, 50)
    board.get(180, 500)
    since = board.request(15, 50)
    board.refresh(trades_as_of=since - 1)  # the tap waits for the next poll, and so does the pass
    assert scorer.calls == [50.0, 500.0] and board.scored_at == 0.0
    board.refresh(trades_as_of=since + 1)
    assert scorer.calls[2] == (50.0, 10)  # the tapped view goes first, with quotes from now
    assert sorted(scorer.calls[3:]) == [100.0, 500.0] and board.scored_at > 0


def test_poll_now_cuts_the_wait_short():
    class Idle:
        def iter_trades(self, *args, **kwargs):
            return iter(())

    polls = []
    col = Collector(Idle(), poll_seconds=30, initial_backfill_minutes=0, after_poll=lambda c: polls.append(c.poll_started))
    col.start()
    deadline = time.time() + 3
    while not polls and time.time() < deadline:
        time.sleep(0.01)
    tap = time.time()
    col.poll_now()
    while len(polls) < 2 and time.time() < deadline:
        time.sleep(0.01)
    col.stop()
    assert len(polls) == 2 and polls[1] >= tap


def test_refresh_end_to_end_with_the_demo_feed():
    """Collector, board and scorer wired as in app.py: a tap gets scores from a poll after it."""
    from combo_tracker.demo import DemoClient
    from combo_tracker.pipeline import Board, Scorer

    class SlowDemo(DemoClient):
        def iter_trades(self, min_ts, max_ts=None, max_pages=0):
            time.sleep(0.05)  # a little network time
            return iter([t for t in self.combo_trades(min_ts)
                         if max_ts is None or datetime.fromisoformat(t["created_time"]) <= max_ts])

    client = SlowDemo()
    col = Collector(client, poll_seconds=30)
    board = Board(Scorer(client), col.window, every=120)
    col.after_poll = lambda c: board.refresh(trades_as_of=c.poll_started)
    col.start()
    deadline = time.time() + 10
    while board.peek(60, 100) is None and time.time() < deadline:
        time.sleep(0.02)
    first = board.peek(60, 100)
    since = board.request(60, 100)
    col.poll_now()
    while not board.done(60, 100, since)[0] and time.time() < deadline:
        time.sleep(0.02)
    col.stop()
    assert board.done(60, 100, since) == (True, None)
    assert board.peek(60, 100) is not first and board.peek(60, 100).rows
