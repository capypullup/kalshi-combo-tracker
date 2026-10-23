import json
from datetime import datetime, timedelta, timezone

import pytest

from combo_tracker.bettors import BettorTracker, Profile
from combo_tracker.demo import DemoClient, demo_rfqs
from combo_tracker.rfq import PLACEHOLDER, Rfq, RfqBook, executions, match, size_fits

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)


def tr(tid, ticker="KXMVE-A", sec=0.0, count=100, yes=0.20, side="yes"):
    return {"trade_id": tid, "ticker": ticker, "taker_side": side, "taker_outcome_side": side,
            "yes_price_dollars": yes, "no_price_dollars": 1 - yes, "count_fp": count,
            "created_time": (T0 + timedelta(seconds=sec)).isoformat()}


def rfq(rid, creator="c1", ticker="KXMVE-A", sec=-3.0, closed=1.0, **size):
    return Rfq(rid, creator, ticker, (T0 + timedelta(seconds=sec)).timestamp(),
               closed=None if closed is None else (T0 + timedelta(seconds=closed)).timestamp(), **size)


def test_executions_group_fills_a_second_apart():
    ex = executions([tr("a", count=60), tr("b", sec=0.5, count=40, yes=0.25), tr("c", sec=30, count=10)])
    big = next(e for e in ex if len(e.trade_ids) == 2)
    assert big.count == 100 and big.price == pytest.approx(0.22)
    assert len(ex) == 2


def test_size_fits_contracts_and_target_cost():
    e = executions([tr("a", count=100, yes=0.2)])[0]
    assert size_fits(rfq("r", contracts=100.0), e)
    assert not size_fits(rfq("r", contracts=90.0), e)
    assert size_fits(rfq("r", target=20.0, target_excludes_fees=True), e)
    assert size_fits(rfq("r", target=21.12), e)  # $20 + $1.12 taker fee
    assert not size_fits(rfq("r", target=30.0), e)


def test_match_unique_is_sure_and_ambiguity_goes_to_closest():
    execs = executions([tr("a"), tr("b", ticker="KXMVE-B", sec=100)])
    out = match([rfq("r1", contracts=100.0), rfq("r2", ticker="KXMVE-B", sec=95, closed=99, contracts=100.0),
                 rfq("r3", creator="c2", ticker="KXMVE-B", sec=10, closed=90, contracts=100.0)], execs)
    by = {m.rfq_id: m for m in out}
    assert by["r1"].sure and by["r1"].execution.trade_ids == ["a"]
    assert by["r2"].execution.trade_ids == ["b"] and not by["r2"].sure  # r3 fits too, but closed 10s earlier
    assert "r3" not in by  # trade b is taken


def test_match_ignores_wrong_size_or_time():
    execs = executions([tr("a", sec=500)])
    assert match([rfq("r1", contracts=100.0)], execs) == []  # long after the RFQ closed
    assert match([rfq("r1", contracts=5.0, sec=499, closed=501)], execs) == []


def test_book_reads_stream_messages_and_reveals_hidden_requesters():
    client = DemoClient()
    book = RfqBook(client, lambda m: [tr("a")])
    book.on_message(json.dumps({"type": "rfq_created", "msg": {
        "id": "r1", "creator_id": PLACEHOLDER, "market_ticker": "KXMVE-A", "contracts_fp": "100.00",
        "created_ts": (T0 - timedelta(seconds=2)).isoformat()}}))
    book.on_message(json.dumps({"type": "rfq_deleted", "msg": {"id": "r1", "deleted_ts": T0.isoformat()}}))
    assert book.rfqs["r1"].closed == T0.timestamp()
    book.keep = 10**9  # T0 is a fixed date; keep it
    out = book.match_now()
    assert len(out) == 1 and out[0].creator != PLACEHOLDER  # looked up with get_rfq after matching
    assert set(book.by_creator()) == {out[0].creator}


def test_tracker_turns_matches_into_code_bettors():
    client = DemoClient()
    book = RfqBook(client, lambda m: client.combo_trades(datetime.now(timezone.utc) - timedelta(minutes=m)))
    for d in demo_rfqs(client):
        book.ingest(d)
    t = BettorTracker(client, rfq=book)
    t.cycle()
    matches = {id(m): m for m in book.matches.values()}.values()
    assert matches and all(m.rfq_id == "rfq-" + m.execution.trade_ids[0] for m in matches if m.sure)
    codes = [r for r in t.snapshot.rows if r["code"]]
    assert codes and all(r["all"]["n"] > 0 and r["pnl_all"] == r["all"]["pnl"] for r in codes)
    assert t.snapshot.rfq["codes"] == len(codes)


def test_link_code_to_public_nickname():
    class Client(DemoClient):
        def social_trades(self, market_ticker=None, **kw):
            return {"trades": [{"trade_id": "x1", "taker_nickname": "combo.king"}]}

    t = BettorTracker(Client())
    p = t.profiles.setdefault("#abc", Profile("#abc", code="abc"))
    p.sources.add("rfq")
    from combo_tracker.bettors import Fill
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
