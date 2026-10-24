import json
import time

import pytest

from combo_tracker import recorder
from combo_tracker.client import KalshiAuth
from combo_tracker.recorder import LOCK, Store, Tally, build_view, pack_live, pack_view, parse_tags, seal, unseal
from combo_tracker.rfq import PLACEHOLDER, Execution, Match

NOW = 1_800_000_000.0  # a fixed "now"
WHALE, FISH, TENNIS_PRO = "a" * 64, "b" * 64, "c" * 64


def match(code, i, size, price=0.5, ts=None, tennis=False, sure=True):
    ts = NOW - 3600 if ts is None else ts
    e = Execution(f"KXMVE-{i}", "yes", ts, round(size / price, 2), price, [f"t{i}"], ts)
    return Match("", code, e, sure, tennis)


def row(fair=0.6, legs=2, hours=5.0, live=False):
    return {"fair_mid": fair, "fair_crx": fair - 0.05, "legs": legs, "hours_to_resolve": hours,
            "leg_detail": [{"side": "yes", "leg": "Lakers", "ticker": "KXNBAGAME-26OCT23LALBOS-LAL", "status": "active",
                            "hours_to_start": -1.0 if live else 2.0}, {"side": "no", "leg": "Over 220.5", "ticker": "KXNBATOTAL-X",
                            "status": "active", "hours_to_start": 2.0}]}


def test_store_keeps_whales_tennis_and_tagged_only():
    s = Store()
    assert s.consider(match(FISH, 1, 900)) is None  # small, not tennis, not tagged
    assert s.consider(match(WHALE, 2, 900)) is None  # not a whale yet
    big = s.consider(match(WHALE, 3, 6000))
    assert big and s.is_whale(WHALE) and s.codes[WHALE]["big"] == 6000
    assert s.consider(match(WHALE, 4, 1500)) is not None  # a whale's later $1k+ bet
    assert s.consider(match(WHALE, 5, 500)) is None  # too small even for a whale
    assert s.consider(match(TENNIS_PRO, 6, 30, tennis=True)) is not None
    assert s.consider(match(TENNIS_PRO, 7, 10, tennis=True)) is None
    assert s.consider(match(PLACEHOLDER, 8, 9000)) is None  # a hidden requester isn't a person we can track
    s.tags.add(FISH)
    assert s.consider(match(FISH, 9, 3)) is not None  # tagged: every bet counts
    assert s.consider(match(FISH, 9, 3)) is None  # the same trade twice
    assert len(s.bets) == 4


def test_price_settle_and_numbers():
    s = Store()
    b = s.consider(match(WHALE, 1, 6000, price=0.5))
    s.price(b, row(fair=0.6, live=True), now=NOW)
    assert b["fm"] == 0.6 and b["L"] == 2 and b["lv"] is True and b["ti"] == "Lakers · NO Over 220.5"
    assert b["x"] == NOW + 5 * 3600 and b["ten"] is False
    stake, ev, var, pnl = recorder.numbers(b)
    assert stake == 6000 and ev == pytest.approx((0.6 - 0.5) * 12000 - 210) and pnl is None
    assert s.unsettled(now=NOW - 3000) == []  # too fresh
    assert s.unsettled(now=NOW) == ["KXMVE-1"]  # a first look, in case a leg already lost
    assert s.settle({"KXMVE-1": {"status": "active", "result": ""}}, now=NOW) == 0
    assert s.unsettled(now=NOW + 3600) == []  # asked; not due to resolve for 5h
    assert s.unsettled(now=NOW + 5 * 3600 + 60) == ["KXMVE-1"]  # past its expected resolution
    assert s.settle({"KXMVE-1": {"status": "finalized", "result": "yes"}}) == 1
    assert recorder.numbers(b)[3] == pytest.approx(0.5 * 12000 - 210)
    s2 = Store()
    v = s2.consider(match(WHALE, 2, 6000))
    assert s2.settle({"KXMVE-2": {"status": "finalized", "result": ""}}) == 1 and v["r"] == "void"


def test_tally_matches_summary_and_folding_keeps_totals():
    s = Store(tennis_min=10)
    bets = []
    for i in range(6):
        b = s.consider(match(TENNIS_PRO, i, 50 + 10 * i, price=0.4 if i % 2 else 0.96, ts=NOW - (10 + i) * 86400, tennis=True))
        s.price(b, row(fair=0.5))
        b["r"] = "yes" if i % 3 else "no"
        bets.append(b)
    whole = s.tally(TENNIS_PRO, "t", locks=True).out()
    assert whole["n"] == 6 and whole["st"] == 6 and whole["pnl"] is not None and whole["z"] is not None
    no_locks = s.tally(TENNIS_PRO, "t").out()
    assert no_locks["n"] == 3  # the 96¢ bets are locks
    assert s.count(TENNIS_PRO) == 6
    folded = s.fold(now=NOW)
    assert folded == 6 and not s.bets and set(s.buckets[TENNIS_PRO]) == {"a", "aL", "t", "tL"}
    after = s.tally(TENNIS_PRO, "t", locks=True).out()
    for k in ("n", "vol", "st", "pnl", "ev", "edge", "win", "avgp"):
        assert after[k] == pytest.approx(whole[k], abs=0.02), k
    assert after["z"] == pytest.approx(whole["z"], abs=0.01)
    assert s.tally(TENNIS_PRO, "t").out()["n"] == 3 and s.count(TENNIS_PRO) == 6
    assert s.tally(TENNIS_PRO, "t", since=NOW - 12.5 * 86400).out()["n"] == 2  # the two newest days
    s.prune(now=NOW + 400 * 86400)
    assert not s.buckets and not s.codes


def test_whales_and_tagged_stay_whole():
    s = Store()
    b = s.consider(match(WHALE, 1, 7000, ts=NOW - 30 * 86400))
    b["r"] = "no"
    s.tags.add("Bendeck18")
    s.name(FISH, "Bendeck18")
    f = s.consider(match(FISH, 2, 20, ts=NOW - 30 * 86400))
    f["r"] = "yes"
    assert s.is_tagged(FISH) and s.fold(now=NOW) == 0 and len(s.bets) == 2


def test_linking_names_a_code():
    s = Store()
    b = s.consider(match(WHALE, 1, 6000, ts=NOW - 600))
    assert s.to_link(now=NOW) == [b]
    assert s.link(b, [{"trade_id": "t1", "taker_nickname": "whale.king"}]) == "whale.king"
    assert s.codes[WHALE]["name"] == "whale.king" and s.code_of("whale.king") == WHALE
    assert s.to_link(now=NOW) == []
    s.consider(match(FISH, 2, 30, ts=NOW - 600, tennis=True))  # one tennis bet: not worth a lookup yet
    assert s.to_link(now=NOW) == []


def test_view_rows_live_and_packing():
    s = Store(tennis_min=10)
    b = s.consider(match(WHALE, 1, 8000, price=0.97, ts=NOW - 7200))  # a lock
    s.price(b, row(fair=0.98))
    b2 = s.consider(match(WHALE, 2, 2000, ts=NOW - 3600))
    s.price(b2, row(fair=0.55))
    for i in range(6):
        t = s.consider(match(TENNIS_PRO, 10 + i, 40, ts=NOW - 3000 - i * 60, tennis=True))
        s.price(t, row(fair=0.58))
        if i < 5:
            t["r"] = "yes"
    s.consider(match(FISH, 20, 30, ts=NOW - 100, tennis=True))  # 1 tennis bet: not listed
    s.note_run(NOW - 86400, NOW)
    view = build_view(s, now=NOW)
    assert view["whales"] == 1 and view["bettors"] == 2 and view["coverage"] == 1.0
    rows = {r["code"]: r for r in view["rows"]}
    whale = rows[WHALE]
    assert whale["w"] and whale["a:all"]["n"] == 1 and whale["a+:all"]["n"] == 2  # locks only with "+"
    assert whale["a:30"]["big"] == 2000 and whale["a+:7"]["big"] == 8000 and "t:all" in whale and "a+:all" not in rows[TENNIS_PRO]
    pro = rows[TENNIS_PRO]
    assert pro["t:30"]["n"] == 6 and pro["t:30"]["st"] == 5 and pro["t:30"]["win"] == 1.0 and pro["t:30"]["z"] > 0
    assert [b["id"] for b in pro["recent"]][:2] == ["t10", "t11"]
    live = {b["id"] for b in view["live"]}
    assert live == {"t1", "t2", "t15"}  # the whale's open bets and the tennis pro's one open bet; the fish isn't watched
    packed = pack_view(view, "t:30", open_code=TENNIS_PRO, tags=[WHALE])
    assert packed["variant"] == "t:30" and packed["detail"]["code"] == TENNIS_PRO and len(packed["detail"]["recent"]) == 6
    by = {r["code"]: r for r in packed["rows"]}
    assert by[WHALE]["tg"] and by[WHALE]["s"]["n"] == 0 and by[TENNIS_PRO]["s"]["n"] == 6
    assert by[WHALE]["big"] >= 5000 and not by[TENNIS_PRO]["tg"]  # stars are the app's, not the recorder's list
    assert pack_view(view, "a+:7")["rows"][0]["s"]  # whales have it; others fall back to the plain variant
    lv = pack_live(view, tags=[TENNIS_PRO])
    assert len(lv["live"]) == 3 and all("t30" in b and "name" in b for b in lv["live"])
    assert next(b for b in lv["live"] if b["c"] == TENNIS_PRO)["tg"]


def test_store_roundtrip_and_sealing():
    s = Store()
    b = s.consider(match(WHALE, 1, 6000, ts=NOW - 60))
    s.tags.add("x" * 64)
    s.note_run(NOW - 100, NOW)
    d = json.loads(json.dumps(s.to_dict()))
    s2 = Store.from_dict(d)
    assert s2.bets == {"t1": b} and s2.tags == s.tags and s2.runs == s.runs and s2.whale_min == 5000
    blob = seal({"saved": 1, "rows": [1, 2, 3]}, secret="pass phrase")
    env = json.loads(blob)
    assert env["key"] == "BETTOR_DATA_KEY" and "rows" not in blob.decode()
    assert unseal(blob, secret="pass phrase") == {"saved": 1, "rows": [1, 2, 3]}
    with pytest.raises(recorder.Locked):
        unseal(blob, secret="other")
    with pytest.raises(recorder.Locked):
        unseal(blob)


def test_sealing_with_the_kalshi_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    auth = KalshiAuth("key-id", pem)
    blob = seal({"a": 1}, auth)
    assert json.loads(blob)["key"] == auth.fingerprint() and unseal(blob, auth) == {"a": 1}


def test_feed_caches_and_reports():
    calls = []

    def fetch():
        calls.append(1)
        return seal({"saved": 5, "rows": []}, secret="k") if len(calls) == 1 else None

    feed = recorder.Feed(secret="k", ttl=1000, fetch=fetch)
    assert feed.get() == {"saved": 5, "rows": []} and feed.get() == {"saved": 5, "rows": []} and len(calls) == 1
    assert feed.get(force=True) == {"saved": 5, "rows": []} and "hasn't saved" in feed.error  # keeps the last good copy
    locked = recorder.Feed(secret="other", fetch=lambda: seal({"x": 1}, secret="k"))
    assert locked.get() is None and "locked with key" in locked.error


def test_tags_parse():
    assert parse_tags(b'{"tags": ["@Bendeck18", " #' + b"a" * 64 + b'"]}') == {"Bendeck18", "a" * 64}
    assert parse_tags(b'["x"]') == {"x"} and parse_tags(None) == set() and parse_tags(b"nope") == set()
    assert recorder.is_code("a" * 64) and not recorder.is_code("Bendeck18")


def test_note_run_joins_close_runs():
    s = Store()
    s.note_run(100, 200)
    s.note_run(700, 900)  # picked up within 15 min: the same stretch
    s.note_run(5000, 5100)
    assert s.runs == [[100, 900], [5000, 5100]]
    t = Tally()
    assert t.out()["n"] == 0 and t.out()["z"] is None


def test_unsettled_backs_off_for_unpriced_and_overdue_bets():
    s = Store()
    b = s.consider(match(WHALE, 1, 6000))  # never priced: no expected resolution
    assert s.unsettled(now=NOW) == ["KXMVE-1"]
    s.settle({"KXMVE-1": {"status": "active", "result": ""}}, now=NOW)
    assert s.unsettled(now=NOW + 1800) == []  # asked half an hour ago: not again yet
    assert s.unsettled(now=NOW + 3601) == ["KXMVE-1"]  # an hour later, yes
    s.price(b, row(hours=1.0), now=NOW)
    s.settle({"KXMVE-1": {"status": "active", "result": ""}}, now=NOW + 3600)  # past its hour, still open
    assert s.unsettled(now=NOW + 3600 + 1500) == []  # overdue bets are asked every 30 min, not every call
    assert s.unsettled(now=NOW + 3600 + 1801) == ["KXMVE-1"]


def test_tally_with_grouped_bets_and_day_boundary():
    s = Store(tennis_min=10)
    for i in range(4):
        b = s.consider(match(TENNIS_PRO, i, 100, price=0.4, ts=NOW - (10 + i) * 86400, tennis=True))
        b["r"] = "yes"
    mine = [b for b in s.bets.values() if b["c"] == TENNIS_PRO]
    since = NOW - 12.5 * 86400
    whole = s.tally(TENNIS_PRO, "t", since=since).out()
    assert whole == s.tally(TENNIS_PRO, "t", since=since, bets=mine).out()
    s.fold(now=NOW)
    assert s.tally(TENNIS_PRO, "t", since=since).out()["n"] == whole["n"]  # folding changes nothing
    assert s.count(TENNIS_PRO) == 4 == s.count(TENNIS_PRO, whole=0)


def test_tags_are_codes_or_nicknames_only():
    assert recorder.valid_tag("Bendeck18") and recorder.valid_tag("a" * 64) and recorder.valid_tag("j.r_0-9")
    assert not recorder.valid_tag("") and not recorder.valid_tag("x" * 33) and not recorder.valid_tag("<b>hi</b>")
    assert parse_tags(b'{"tags": ["ok", "<script>", "a b"]}') == {"ok"}
    assert parse_tags(b'{"tags": "ok"}') == set()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self.ok, self._body = status, status < 400, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class _Session:
    """Just enough of requests.Session for GitHubTags: one file on one branch."""

    def __init__(self, tags):
        self.headers, self.tags, self.puts = {}, set(tags), 0

    def get(self, url, **kw):
        if url.endswith("/tags.json"):
            import base64

            return _Resp(200, {"content": base64.b64encode(json.dumps({"tags": sorted(self.tags)}).encode()).decode(),
                               "sha": "abc"})
        return _Resp(200, {})

    def put(self, url, json=None, **kw):
        self.puts += 1
        return _Resp(200)


def test_github_tags_refuses_junk_and_too_many():
    gh = recorder.GitHubTags("t", session=_Session([f"u{i}" for i in range(recorder.MAX_TAGS)]))
    with pytest.raises(ValueError):
        gh.toggle("<script>")
    with pytest.raises(ValueError):
        gh.toggle("one.more")
    assert "u1" not in gh.toggle("u1") and gh.session.puts == 1  # unstarring always works
    gh = recorder.GitHubTags("t", session=_Session(["u1"]))
    assert gh.set("u1", True) == {"u1"} and gh.session.puts == 0  # already starred: nothing to write
    assert gh.set("u1", False) == set() and gh.session.puts == 1
    assert gh.set("u2", None) == {"u1", "u2"} and gh.session.puts == 2  # None flips (the fake file still holds u1)


def test_fetch_and_push_files_keep_a_lease(tmp_path):
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    works = []
    for name in ("a", "b"):
        w = tmp_path / name
        w.mkdir()
        subprocess.run(["git", "init", "-q", str(w)], check=True)
        subprocess.run(["git", "-C", str(w), "remote", "add", "origin", str(bare)], check=True)
        works.append(str(w))
    a, b = works
    assert recorder.fetch_files(cwd=a) == {}  # no branch yet, not an error
    recorder.push_files({"store.json": b"one"}, "first", cwd=a)
    assert recorder.fetch_files(cwd=b) == {"store.json": b"one"}
    recorder.push_files({"store.json": b"two"}, "second", cwd=b)  # b has fetched the branch: fine
    with pytest.raises(recorder.GitError, match="changed under us"):
        recorder.push_files({"store.json": b"three"}, "stale", cwd=a)  # a still holds the first commit
    assert recorder.fetch_files(cwd=a) == {"store.json": b"two"}
    recorder.push_files({"store.json": b"three"}, "fresh", cwd=a)  # after a fetch, a may write again
    assert recorder.fetch_files(cwd=b, names=("store.json", "missing.json")) == {"store.json": b"three"}
    subprocess.run(["git", "-C", a, "remote", "set-url", "origin", str(tmp_path / "gone.git")], check=True)
    with pytest.raises(recorder.GitError, match="fetching"):
        recorder.fetch_files(cwd=a)  # a broken remote is an error, never "no data yet"


def test_financial_legs_are_dropped_and_purged():
    s = Store()
    b = s.consider(match(WHALE, 1, 6000))
    s.price(b, row())
    assert b["lt"] == ["KXNBAGAME-26OCT23LALBOS-LAL", "KXNBATOTAL-X"]  # the legs' tickers travel with the bet
    s.consider(match(WHALE, 2, 9000))  # never priced: its legs are unknown until asked
    assert s.legs_unknown() == ["KXMVE-2"]
    assert s.set_legs({"KXMVE-2": {"mve_selected_legs": [{"market_ticker": "KXBTC15M-1", "side": "yes"}]}}) == 1
    assert s.legs_unknown() == [] and s.codes[WHALE]["big"] == 9000
    assert s.purge(lambda t: t.startswith("KXBTC")) == 1
    assert set(s.bets) == {"t1"} and s.codes[WHALE]["big"] == 6000  # the whale's size no longer counts the BTC bet
    s.drop(b)
    assert not s.bets and WHALE not in s.codes
    f = recorder.financial_filter()
    assert f is None or callable(f)
