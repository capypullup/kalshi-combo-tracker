import json
from datetime import datetime, timedelta, timezone

import pytest

from combo_tracker.bettors import BettorTracker, Fill, Profile
from combo_tracker.demo import DemoClient, demo_rfq_book
from combo_tracker.rfq import PLACEHOLDER, Execution, Match, RfqBook, executions, partly_fits, pick, size_fits

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)
T = T0.timestamp()


def tr(tid, ticker="KXMVE-A", sec=0.0, count=100, yes=0.20, side="yes"):
    return {"trade_id": tid, "ticker": ticker, "taker_side": side, "taker_outcome_side": side,
            "yes_price_dollars": yes, "no_price_dollars": 1 - yes, "count_fp": count,
            "created_time": (T0 + timedelta(seconds=sec)).isoformat()}


def close(sec, creator="c1", contracts=100.0, target=None, rid=None):
    return (T + sec, creator, contracts, target, rid)


def test_executions_group_fills_a_second_apart():
    ex = executions([tr("a", count=60), tr("b", sec=0.5, count=40, yes=0.25), tr("c", sec=30, count=10)])
    big = next(e for e in ex if len(e.trade_ids) == 2)
    assert big.count == 100 and big.price == pytest.approx(0.22)
    assert (big.ts, big.end) == (T, T + 0.5)
    assert [(p.count, p.trade_ids) for p in big.parts] == [(60, ["a"]), (40, ["b"])]
    assert len(ex) == 2


def test_size_fits_contracts_and_target_cost():
    e = executions([tr("a", count=100, yes=0.2)])[0]
    assert size_fits(100.0, None, e)
    assert not size_fits(90.0, None, e)
    assert size_fits(None, 21.12, e)  # $20 + $1.12 taker fee
    assert size_fits(None, 20.0, e)  # asked for the cost before fees
    assert not size_fits(None, 30.0, e)
    assert not size_fits(None, None, e)
    assert partly_fits(None, 30.0, e) and partly_fits(120.0, None, e)  # a quote filled less than asked
    assert not partly_fits(None, 60.0, e) and not partly_fits(90.0, None, e)


def test_pick_is_sure_when_every_fit_is_one_requester():
    e = executions([tr("a")])[0]
    slip = [close(-10), close(-1.2), close(0.05)]  # the app re-asked while the slip was open
    assert pick(slip, e) == (close(0.05), True, True)
    assert pick(slip + [close(1.5, creator="c2")], e)[1]  # someone else's slip refreshing a second later
    best, sure, _ = pick(slip + [close(0.3, creator="c2")], e)  # someone else closed the same bet just then
    assert best == close(0.05) and not sure
    assert pick([close(0.0, contracts=5.0)], e) == (None, False, False)  # wrong size
    assert pick([close(0.1, contracts=120.0)], e) == (close(0.1, contracts=120.0), True, False)  # filled less
    assert pick([close(0.1, contracts=120.0), close(0.2, creator="c2")], e)[0] == close(0.2, creator="c2")
    assert pick([close(-30), close(6)], e) == (None, False, False)  # closed too early / too late
    best, _, _ = pick(slip, e, used={("KXMVE-A", T + 0.05)})  # that close already went to another trade
    assert best == close(-1.2)


def test_pick_hidden_requesters_are_sure_only_alone():
    e = executions([tr("a")])[0]
    assert pick([close(0.1, creator=PLACEHOLDER, rid="r1")], e)[1]
    best, sure, _ = pick([close(0.1, creator=PLACEHOLDER, rid="r1"), close(0.3, creator=PLACEHOLDER, rid="r0")], e)
    assert best[4] == "r1" and not sure


def created(ticker="KXMVE-A", legs=()):
    return json.dumps({"type": "rfq_created", "sid": 1, "seq": 1, "msg": {
        "id": "r1", "creator_id": "c1", "market_ticker": ticker, "created_ts": T0.isoformat(),
        "mve_selected_legs": list(legs)}}, separators=(",", ":"))


def test_book_reads_stream_messages():
    book = RfqBook(DemoClient())
    assert book.on_message(created()) is None
    tennis_leg = {"event_ticker": "KXATPMATCH-26OCT22WOLJON", "market_ticker": "KXATPMATCH-26OCT22WOLJON-WOL",
                  "side": "yes"}
    book.on_message(created("KXMVE-T", [tennis_leg]))
    assert book.stats["rfqs"] == 2 and set(book.tennis) == {"KXMVE-T"}
    book.on_message(json.dumps({"type": "rfq_deleted", "sid": 1, "seq": 2, "msg": {
        "id": "r1", "creator_id": "c1", "market_ticker": "KXMVE-A", "target_cost_dollars": "10.0000",
        "deleted_ts": T0.isoformat()}}))
    assert book.closes["KXMVE-A"] == [(T, "c1", None, 10.0, None)] and book.held() == 1
    assert book.on_message('{"type":"error","sid":1,"msg":{"code":25,"msg":"Subscription buffer overflow"}}') is None
    assert book.stats["overflows"] == 1
    assert book.on_message('{"type":"unsubscribed","sid":1,"seq":3}') == "resubscribe"


def test_match_trades_waits_for_late_fills_and_late_closes():
    book = RfqBook(DemoClient(), settle=3, wait=10)
    book.add_close("KXMVE-A", T + 0.2, "c1", 100.0, None)
    assert book.match_trades([tr("a")], now=T + 1) == []  # more fills of it may still come
    found = book.match_trades([], now=T + 4)
    assert [m.execution.trade_ids for m in found] == [["a"]] and found[0].sure and found[0].creator == "c1"
    assert book.take() == found and book.take() == []

    assert book.match_trades([tr("b", ticker="KXMVE-B", sec=10)], now=T + 14) == []  # no close yet
    book.add_close("KXMVE-B", T + 10.1, "c2", 100.0, None)
    assert [m.creator for m in book.match_trades([], now=T + 16)] == ["c2"]

    book.match_trades([tr("c", ticker="KXMVE-C", sec=20)], now=T + 24)
    book.match_trades([], now=T + 40)  # waited long enough: no RFQ for it
    assert book.stats["executions"] == 3 and book.stats["sure"] == 2 and book.stats["unmatched"] == 1
    assert book.missed[-1][0].trade_ids == ["c"] and book.missed[-1][1] is None


def test_two_bets_a_moment_apart_are_matched_one_by_one():
    book = RfqBook(DemoClient(), settle=0)
    book.add_close("KXMVE-A", T + 0.1, "c1", 25.0, None)
    book.add_close("KXMVE-A", T + 0.6, "c2", 75.0, None)
    found = book.match_trades([tr("a", count=25), tr("b", sec=0.5, count=75)], now=T + 5)
    assert sorted((m.creator, m.execution.trade_ids[0]) for m in found) == [("c1", "a"), ("c2", "b")]
    assert book.stats["split"] == 1 and book.stats["executions"] == 1


def test_hidden_requester_is_looked_up_after_matching():
    book = RfqBook(DemoClient(), settle=0)
    book.add_close("KXMVE-A", T + 0.2, PLACEHOLDER, 100.0, None, rfq_id="r1")
    book.match_trades([tr("a")], now=T + 1)
    assert book.take() == [] and book.stats["hidden"] == 1  # waits for its real ID
    book.reveal(now=T + 2)
    out = book.take()
    assert len(out) == 1 and out[0].creator != PLACEHOLDER and book.stats["revealed"] == 1


def test_prune_keeps_a_short_window():
    book = RfqBook(DemoClient(), keep_seconds=45)
    book.add_close("KXMVE-A", T, "c1", 100.0, None)
    book.add_close("KXMVE-A", T + 50, "c1", 100.0, None)
    book.add_close("KXMVE-B", T, "c1", 100.0, None)
    book.prune(now=T + 60)
    assert dict(book.closes) == {"KXMVE-A": [(T + 50, "c1", 100.0, None, None)]}


def test_demo_book_names_the_right_requester():
    book = demo_rfq_book(DemoClient())
    found = list(book.recent)
    assert book.stats["sure"] > book.stats["likely"] > 0 and book.stats["unmatched"] > 0
    # In the demo, an RFQ id names its trade: every match took the RFQ that closed with it, or a re-ask of it.
    assert all(m.rfq_id.startswith(f"rfq-{m.execution.trade_ids[0]}") for m in found if m.rfq_id)


def test_tracker_turns_matches_into_code_bettors():
    client = DemoClient()
    t = BettorTracker(client, rfq=demo_rfq_book(client))
    t.cycle()
    codes = [r for r in t.snapshot.rows if r["code"]]
    assert codes and all(r["all"]["n"] >= 3 and r["pnl_all"] == r["all"]["pnl"] for r in codes)
    assert t.snapshot.rfq["codes"] == len(t.code_profiles()) >= len(codes) == t.snapshot.rfq["shown"]
    assert sum(p.sure for p in t.code_profiles()) > 0


class FakeBook:
    def __init__(self, matches):
        self.matches = matches

    def take(self):
        out, self.matches = self.matches, []
        return out


def bet(creator, i, stake, tennis=False):
    return Match("", creator, Execution(f"KXMVE-{i}", "yes", T + i, stake / 0.5, 0.5, [f"t{i}"], T + i), True, tennis)


def test_only_the_biggest_codes_are_listed_and_memory_is_capped():
    bets = [bet(f"c{k}", 10 * k + j, 100 * k) for k in range(1, 9) for j in range(3)]
    bets += [bet("tennis.guy", 900 + j, 5, tennis=True) for j in range(3)]
    t = BettorTracker(DemoClient(), rfq=FakeBook(bets), top_codes=2, top_tennis_codes=1, max_codes=5)
    assert t.sync_codes() == len(bets)
    assert sorted(p.nickname for p in t.top_codes()) == ["#c7", "#c8", "#tennis.guy"]
    assert len(t.code_profiles()) == 4  # capped to 80% of max_codes, keeping the listed ones
    assert {"#c7", "#c8", "#tennis.guy"} <= {p.nickname for p in t.code_profiles()}
    t.build_rows()
    assert sorted(r["nick"] for r in t.build_rows() if r["code"]) == ["#c7", "#c8", "#tennis.guy"]


def test_link_code_to_public_nickname():
    class Client(DemoClient):
        def social_trades(self, market_ticker=None, **kw):
            return {"trades": [{"trade_id": "x1", "taker_nickname": "combo.king"}]}

    t = BettorTracker(Client(), code_min_bets=1)
    p = t.profiles.setdefault("#abc", Profile("#abc", code="abc"))
    p.sources.add("rfq")
    p.fills["x1"] = Fill("x1", 0, "KXMVE-A", "yes", 0.2, 10, True)
    t.link_codes()
    assert p.linked == "combo.king" and "rfq" in t.profiles["combo.king"].sources
    assert "combo.king" in [c.nickname for c in t.candidates()]


def test_long_history_loads_over_several_passes():
    t = BettorTracker(DemoClient(), pages_per_pass=1)
    p = t.profiles.setdefault("JohnR0809", Profile("JohnR0809"))
    t.refresh_history(p, force=True)
    assert len(p.fills) == 100 and not p.history_done
    for _ in range(10):
        t.refresh_history(p, force=True)
    assert p.history_done and len(p.fills) > 100


def test_poll_reads_each_combo_trade_once():
    class Client(DemoClient):
        feed = [tr("a"), tr("x", ticker="KXNBAGAME-26OCT22DENOKC-OKC")]

        def iter_trades(self, min_ts, **kw):
            return [t for t in self.feed if datetime.fromisoformat(t["created_time"]) >= min_ts]

    book = RfqBook(Client(), settle=0)
    book._since = T - 1
    book.add_close("KXMVE-A", T + 0.1, "c1", 100.0, None)
    assert [m.creator for m in book.poll_trades(now=T + 1)] == ["c1"]
    assert book.poll_trades(now=T + 2) == [] and book.stats["executions"] == 1  # read already; not a combo
