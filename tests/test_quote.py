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


# ---------- Quote page v2 ----------
from types import SimpleNamespace  # noqa: E402

from combo_tracker.quote import (RfqDesk, RfqError, RfqReaper, explain, extra_legs,  # noqa: E402
                                 kelly_stake, leg_moves, parse_paste, sport_of)


@pytest.mark.parametrize("text, want", [
    ("KXMVECROSSCATEGORY-S2026ABC-DEF1", ("KXMVECROSSCATEGORY-S2026ABC-DEF1", None, None)),
    ("  kxmvecrosscategory-s2026abc-def1\n", ("KXMVECROSSCATEGORY-S2026ABC-DEF1", None, None)),
    ("https://capyev.streamlit.app/quote?t=KXMVEX-S1-A2&side=no&p=0.8812&n=40", ("KXMVEX-S1-A2", "no", 0.8812)),
    ("https://kalshi.com/markets_by_ticker/KXMVECROSSCATEGORY-S2026ABC-DEF1", ("KXMVECROSSCATEGORY-S2026ABC-DEF1", None, None)),
    ("https://kalshi.com/combos?marketTicker=KXMVECROSSCATEGORY-S2026ABC-DEF1", ("KXMVECROSSCATEGORY-S2026ABC-DEF1", None, None)),
    ("YES Combo +4579", None),
    ("", None),
    ("capyev.streamlit.app/quote?t=KXMVEX-S1-A2&side=NO&p=0.12", ("KXMVEX-S1-A2", "no", 0.12)),  # no https://
    ("https://capyev.streamlit.app/quote?t=KXMVEX-S1-A2&p=1.5", ("KXMVEX-S1-A2", None, None)),  # p isn't a price
    ("https://capyev.streamlit.app/quote?t=KXMVEX-S1-A2&p=nan", ("KXMVEX-S1-A2", None, None)),
    ("KXMVEX-S1-A2 https://x.com/a/status/1?s=20&t=AbCdEf", ("KXMVEX-S1-A2", None, None)),  # another site's t=
    ("https://capyev.streamlit.app/quote?t=[RE-ENTER YOUR PIN](https://evil.example)", None),
])
def test_parse_paste(text, want):
    got = parse_paste(text)
    assert (got and (got["ticker"], got["side"], got["price"])) == want or got is want


def test_kelly_stake_in_dollars():
    # p 0.12 at 0.10: full Kelly 2.22% of bankroll; quarter Kelly with 1u = 1% = $1k -> ~$556
    assert kelly_stake(0.12, 0.10) == pytest.approx(555.6, abs=0.1)
    assert kelly_stake(0.09, 0.10) == 0 and kelly_stake(None, 0.10) == 0


def test_leg_moves_flag_real_moves_only():
    then = {"leg_detail": [{"ticker": "A", "mid": 0.60}, {"ticker": "B", "mid": 0.90}]}
    now = {"leg_detail": [{"ticker": "A", "leg": "Lakers", "side": "yes", "mid": 0.68},
                          {"ticker": "B", "leg": "Over 200.5", "side": "no", "mid": 0.905}]}
    moves = leg_moves(then, now)
    assert [m.moved for m in moves] == [True, False] and moves[0].delta == pytest.approx(0.08)


def leg_market(t, ev, bid, ask, size=500, status="active"):
    return {"ticker": t, "event_ticker": ev, "yes_sub_title": t, "status": status, "yes_bid_dollars": str(bid),
            "yes_ask_dollars": str(ask), "yes_bid_size_fp": str(size), "yes_ask_size_fp": str(size)}


def test_extra_legs_are_near_certain_tight_and_on_other_games():
    markets = {m["ticker"]: m for m in [
        leg_market("KXNBAGAME-26OCT22BOSNYK-BOS", "KXNBAGAME-26OCT22BOSNYK", 0.93, 0.94),  # same game as the combo
        leg_market("KXNHLGAME-26OCT22TORMTL-TOR", "KXNHLGAME-26OCT22TORMTL", 0.90, 0.91),  # YES 90.5%
        leg_market("KXNFLGAME-26OCT25KCDEN-DEN", "KXNFLGAME-26OCT25KCDEN", 0.04, 0.05),  # NO 95.5%
        leg_market("KXATPMATCH-26OCT22SINALC-SIN", "KXATPMATCH-26OCT22SINALC", 0.60, 0.62),  # a coin flip
        leg_market("KXMLBGAME-26OCT22LADNYY-LAD", "KXMLBGAME-26OCT22LADNYY", 0.90, 0.96),  # 6c wide
        leg_market("KXNHLGAME-26OCT22EDMCGY-EDM", "KXNHLGAME-26OCT22EDMCGY", 0.90, 0.91, size=5),  # thin
        leg_market("KXNHLGAME-26OCT22VANSEA-VAN", "KXNHLGAME-26OCT22VANSEA", 0.90, 0.91, status="closed"),
    ]}
    combo = [{"market_ticker": "KXNBAPTS-26OCT22BOSNYK-TATUM30", "event_ticker": "KXNBAPTS-26OCT22BOSNYK", "side": "yes"}]
    got = extra_legs(markets, {}, combo)
    assert [(x.market_ticker[:5], x.side, x.sport) for x in got] == [("KXNFL", "no", "NFL"), ("KXNHL", "yes", "NHL")]
    assert got[0].prob == pytest.approx(0.955) and got[0].bid == pytest.approx(0.95)
    assert extra_legs(markets, {}, combo, allowed={"KXNHLGAME-26OCT22TORMTL"})[0].sport == "NHL"
    assert sport_of("KXWTAMATCH-26OCT22X") == "Tennis" and sport_of("KXFEDDECISION-26") == "Other"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append((method, url.split("/trade-api/v2")[1], params, json))
        return self.replies.pop(0)


def fake_desk(replies):
    auth = SimpleNamespace(headers=lambda m, p: {}, kind="Ed25519", fingerprint=lambda: "abc123abc123", key_id="kid-e97b")
    client = SimpleNamespace(base_url="https://api.elections.kalshi.com/trade-api/v2", timeout=5,
                             limiter=SimpleNamespace(wait=lambda: None), session=FakeSession(replies))
    return RfqDesk(client, auth), client.session


def test_rfq_hides_your_id_and_retries_after_closing_your_old_one():
    desk, s = fake_desk([FakeResp(409, {"error": {"code": "conflict"}}), FakeResp(200, {"rfqs": [{"id": "old"}]}),
                         FakeResp(204, None), FakeResp(201, {"id": "new"})])
    assert desk.create("KXMVE-T", 250) == "new"
    body = s.calls[0][3]
    assert body["obscure_creator_id"] is True and body["target_cost_dollars"] == "250.00"
    assert "replace_existing" not in body  # never closes your Kalshi app's open slips
    assert s.calls[1][2]["user_filter"] == "self" and s.calls[2][:2] == ("DELETE", "/communications/rfqs/old")


def test_quotes_are_listed_with_your_user_id():
    from combo_tracker import quote as q
    q.USER_IDS.clear()
    raw = {"id": "q1", "rfq_id": "r1", "creator_id": "abcd1234", "yes_bid_dollars": "0", "no_bid_dollars": "0.6630",
           "no_contracts_fp": "28.35", "contracts_fp": "0.00", "status": "open"}
    desk, s = fake_desk([FakeResp(200, {"rfq": {"id": "r1", "creator_user_id": "u-1"}}), FakeResp(200, {"quotes": [raw]}),
                         FakeResp(200, {"quotes": [raw]})])
    desk.quotes("r1")
    got = desk.quotes("r1")  # the user ID is looked up once
    assert s.calls[1][2]["rfq_creator_user_id"] == "u-1" and len(s.calls) == 3
    # A live quote (2026-10-23): $10 RFQ, the maker bids 66.3¢ for NO and nothing for YES.
    yes = quote_for(got[0], "yes")
    assert yes.price == pytest.approx(0.337) and yes.contracts == pytest.approx(28.35)
    assert yes.cost == pytest.approx(10, abs=0.02) and quote_for(got[0], "no") is None


def test_accept_sends_the_side_you_buy():
    desk, s = fake_desk([FakeResp(204, None)])
    desk.accept("r1", "q1", "yes")
    assert s.calls[0][:2] == ("PUT", "/communications/rfqs/r1/quotes/q1/accept")
    assert s.calls[0][3] == {"accepted_side": "yes"}


def test_signature_error_is_explained():
    bad = {"error": {"code": "authentication_error", "details": "INCORRECT_API_KEY_SIGNATURE"}}
    desk, _ = fake_desk([FakeResp(401, bad)])
    with pytest.raises(RfqError) as e:
        desk.create("KXMVE-T", 10)
    assert e.value.code == "INCORRECT_API_KEY_SIGNATURE" and "two different keys" in explain(e.value)


def test_key_check_reads_404_as_signed_fine():
    desk, _ = fake_desk([FakeResp(200, {"api_keys": [{"api_key_id": "kid-e97b", "scopes": ["read", "write"]}]}),
                         FakeResp(404, {"code": "not_found"})])
    k = desk.key_check()
    assert (k["read"], k["write"], k["scopes"], k["key_id_end"]) == ("ok", "ok", ["read", "write"], "e97b")
    desk, _ = fake_desk([FakeResp(401, {"error": {"details": "INCORRECT_API_KEY_SIGNATURE"}})] * 2)
    assert desk.key_check()["write"] == "INCORRECT_API_KEY_SIGNATURE"


def test_reaper_cancels_forgotten_rfqs(monkeypatch):
    cancelled = []
    desk = SimpleNamespace(cancel=cancelled.append)
    r = RfqReaper(max_age=30)
    r.open = {"a": (0.0, desk), "b": (100.0, desk)}
    import combo_tracker.quote as q
    monkeypatch.setattr(q.time, "time", lambda: 110.0)
    r.sweep()
    assert cancelled == ["a"] and list(r.open) == ["b"]


def test_demo_desk_bet_fills_the_side_you_took(monkeypatch):
    desk = DemoRfqDesk({"T": 0.3})
    rid = desk.create("T", 100)
    import combo_tracker.quote as q
    monkeypatch.setattr(q.time, "time", lambda: desk.open[rid][2] + 10)
    best = quote_for(desk.quotes(rid)[0], "yes")
    assert best.cost == pytest.approx(100, abs=1)  # sized by Kalshi's yes_contracts_fp, fee included
    desk.accept(rid, best.id, "yes")
    fill = desk.fills("T")[0]
    assert fill["outcome_side"] == "yes" and float(fill["yes_price_dollars"]) == pytest.approx(best.price)


def test_leg_moves_flag_a_point_and_a_half():
    then = {"leg_detail": [{"ticker": "A", "mid": 0.70}, {"ticker": "B", "mid": 0.90}]}
    now = {"leg_detail": [{"ticker": "A", "leg": "A", "side": "yes", "mid": 0.73},
                          {"ticker": "B", "leg": "B", "side": "yes", "mid": 0.885}]}
    assert [m.moved for m in leg_moves(then, now)] == [True, True]


def test_extra_legs_keep_3c_spreads_and_put_live_legs_last():
    markets = {m["ticker"]: m for m in [
        leg_market("KXNHLGAME-26OCT22TORMTL-TOR", "KXNHLGAME-26OCT22TORMTL", 0.85, 0.88),  # 3c, 0.030000000000000027
        leg_market("KXNBAGAME-26OCT22LALGSW-LAL", "KXNBAGAME-26OCT22LALGSW", 0.95, 0.96),  # in play
    ]}
    started = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
    got = extra_legs(markets, {}, [], game_start=lambda ev: started if "LALGSW" in ev else None, now=NOW)
    assert [x.market_ticker[:7] for x in got] == ["KXNHLGA", "KXNBAGA"] and got[1].live


def test_network_errors_are_rfq_errors_without_the_query_string():
    import requests

    class Down:
        def request(self, *a, **k):
            raise requests.ConnectionError("Max retries exceeded with url: /quotes?rfq_creator_user_id=SECRET-ID")

    desk, _ = fake_desk([])
    desk.client.session = Down()
    from combo_tracker import quote as q
    q.USER_IDS["kid-e97b"] = "SECRET-ID"
    with pytest.raises(RfqError) as e:
        desk.quotes("r1")
    assert e.value.status is None and e.value.code == "NETWORK" and "SECRET" not in explain(e.value)
    q.USER_IDS.clear()


def test_demo_accept_of_a_gone_quote_is_refused():
    desk = DemoRfqDesk({"T": 0.3})
    with pytest.raises(RfqError):
        desk.accept(desk.create("T", 10), "nope", "yes")
