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
