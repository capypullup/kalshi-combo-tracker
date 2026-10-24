"""The alert settings: the Settings object, the file the app saves and the bot re-reads, and the Alerts tool."""

import base64
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from alert_bets import bets
from combo_tracker import alerts, alerts_view
from combo_tracker.alerts import Settings
from combo_tracker.ghfile import GitHubFile, MemoryFile

APP = str(Path(__file__).with_name("alerts_app.py"))


def test_settings_round_trip_and_defaults():
    s = Settings()
    assert s.to_dict() == {"pregame": "50:100,500:20,1000:10", "live": "1000:100,5000:50", "basis": "mid",
                           "skip_thin": True, "thin_spread_cents": 3, "thin_size": 50, "skip_same_game": True,
                           "max_legs": 5, "max_legs_live": 3, "max_leagues": 2}
    assert Settings.from_dict(s.to_dict()) == s
    odd = Settings(pregame=[(250, 0.333)], live=[], basis="crx", skip_thin=False, thin_spread=0.025, thin_size=20,
                   skip_same_game=False, max_legs=None, max_legs_live=4, max_leagues=None)
    assert Settings.from_dict(json.loads(json.dumps(odd.to_dict()))) == odd
    assert odd.to_dict()["live"] == "off" and odd.min_bet() == 250 and Settings(pregame=[], live=[]).min_bet() is None
    assert alerts.tiers_text(alerts.parse_tiers("1000:10,500:20.5")) == "500:20.5,1000:10"


def test_settings_from_a_file_change_only_what_it_has_and_refuse_nonsense():
    base = Settings(max_legs=4)
    s = Settings.from_dict({"max_legs_live": "off", "skip_same_game": False, "saved_at": "2026-10-24T03:00:00+00:00"}, base)
    assert (s.max_legs, s.max_legs_live, s.skip_same_game, s.pregame) == (4, None, False, base.pregame)
    for bad in ({"max_legs": 0}, {"max_legs": 2.5}, {"max_legs": True}, {"pregame": "500"}, {"pregame": "0:20"},
                {"live": "x:1"}, {"basis": "best"}, {"skip_thin": "maybe"}, {"thin_size": -1}, {"thin_spread_cents": "?"}):
        with pytest.raises(ValueError):
            Settings.from_dict(bad)


def test_settings_from_the_bots_variables():
    env = {"ALERT_TIERS_LIVE": "off", "ALERT_SKIP_SAME_GAME": "0", "ALERT_MAX_LEGS": "off", "ALERT_MAX_LEAGUES": " 3 ",
           "ALERT_EV_BASIS": "CRX", "ALERT_TIERS_PREGAME": ""}
    s = Settings.from_env(env)
    assert (s.live, s.skip_same_game, s.max_legs, s.max_leagues, s.basis) == ([], False, None, 3, "crx")
    assert s.pregame == Settings().pregame and Settings.from_env({}) == Settings()
    assert s.describe().startswith("EV CRX pregame: $50+ at +100%, $500+ at +20%, $1,000+ at +10% · live: off · no thin books")
    assert "same-game" not in s.describe() and "up to 3 leagues" in s.describe()
    assert "(over 2.5¢ or under 50)" in Settings(thin_spread=0.025).describe()
    assert Settings.from_env({"ALERT_SKIP_SAME_GAME": "yes"}).skip_same_game is True  # anything but 0 keeps it on
    for bad in ("nan", "inf"):
        with pytest.raises(ValueError):
            alerts.parse_tiers(f"500:{bad}")
        with pytest.raises(ValueError):
            Settings.from_dict({"thin_size": bad})


def test_filters_follow_the_settings_and_each_one_counts():
    rows = {r["ticker"]: r for r in bets()}
    thin, six = rows["KXMVECOMBO-THIN"], dict(rows["KXMVECOMBO-LIVE4"])
    assert alerts.filtered_by(thin, Settings()) == ["thin"]
    assert alerts.filtered_by(thin, Settings(thin_spread=0.06)) == []  # a 5¢ spread is fine under a 6¢ bar
    assert "thin leg book" in alerts.details(thin) and "thin leg book" not in alerts.details(thin, (0.06, 50))
    six["legs"] = 6  # a 6-leg live combo trips both leg caps, so switching one off still leaves it out
    assert alerts.filtered_by(six, Settings()) == ["legs", "legs_live"]


class Store:
    def __init__(self, *answers):
        self.answers = list(answers)

    def read(self):
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def test_the_bot_takes_each_save_and_keeps_going_through_bad_ones():
    base, log = Settings(max_legs=4), []
    feed = alerts.AppSettings(Store((None, None), ({"max_legs": 6}, "a"), ({"max_legs": 6}, "a"), ConnectionError("down"),
                                    ConnectionError("down"), ({"max_legs": "lots"}, "b"), (None, None)), base, log.append)
    assert feed.get() is base and log == [f"no app settings: {base.describe()}"]
    assert feed.get().max_legs == 6 and len(log) == 2 and log[1].startswith("settings from the app: ")
    assert feed.get().max_legs == 6 and len(log) == 2  # same sha: nothing new
    assert feed.get().max_legs == 6 and feed.get().max_legs == 6 and len(log) == 3 and "couldn't read" in log[2]
    assert feed.get().max_legs == 6 and "don't make sense" in log[3]  # a bad save keeps the last good one
    assert feed.get() is base  # the file's gone: back to the variables


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class GitHub:
    """Just enough of the contents and refs API for GitHubFile."""

    def __init__(self, branch=False, file=None, conflict=False):
        self.headers, self.branch, self.file, self.conflict, self.calls = {}, branch, file, conflict, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        if url.startswith("https://raw.githubusercontent.com/"):
            return Resp(200, self.file) if self.file is not None else Resp(404)
        if "/contents/" in url:
            if self.file is None:
                return Resp(404)
            body = base64.b64encode(json.dumps(self.file).encode()).decode()
            return Resp(200, {"content": body, "sha": f"sha{len(self.calls)}"})
        if url.endswith("/git/ref/heads/alert-settings"):
            return Resp(200 if self.branch else 404)
        if url.endswith("/git/ref/heads/main"):
            return Resp(200, {"object": {"sha": "main-head"}})
        return Resp(200, {"default_branch": "main"})

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        self.branch = True
        return Resp(201)

    def put(self, url, json=None, timeout=None):
        self.calls.append(("PUT", url, json))
        if self.conflict:
            self.conflict = False
            return Resp(409)
        self.file = __import__("json").loads(base64.b64decode(json["content"]))
        return Resp(201)


def test_github_file_makes_the_branch_on_the_first_save_then_writes_over_it():
    gh = GitHub()
    f = GitHubFile("alert_settings.json", "alert-settings", token="t", repo="o/r", session=gh)
    assert f.read() == (None, None) and gh.headers["Authorization"] == "Bearer t"
    f.write({"max_legs": 4}, "Alert settings from the app")
    assert ("POST", "https://api.github.com/repos/o/r/git/refs", {"ref": "refs/heads/alert-settings", "sha": "main-head"}) in gh.calls
    put = [c for c in gh.calls if c[0] == "PUT"][-1]
    assert put[2]["branch"] == "alert-settings" and "sha" not in put[2] and f.read()[0] == {"max_legs": 4}
    gh.conflict = True  # saved from another phone meanwhile: the second try takes the new sha
    f.write({"max_legs": 6}, "again")
    puts = [c for c in gh.calls if c[0] == "PUT"]
    assert len(puts) == 3 and puts[-1][2]["sha"].startswith("sha") and gh.file == {"max_legs": 6}


def test_github_file_without_a_token_reads_the_raw_file_and_cant_write():
    gh = GitHub(branch=True, file={"max_legs": 3})
    f = GitHubFile("alert_settings.json", "alert-settings", repo="o/r", session=gh)
    assert f.read() == ({"max_legs": 3}, None) and gh.calls == [("GET", "https://raw.githubusercontent.com/o/r/alert-settings/alert_settings.json")]
    with pytest.raises(RuntimeError):
        f.write({}, "nope")


def test_draft_round_trips_and_switched_off_caps_keep_their_number():
    for s in (Settings(), Settings(pregame=[], live=[(2000, 0.75)], max_legs=None, max_legs_live=None, max_leagues=None,
                                   skip_thin=False, thin_spread=0.02, basis="crx")):
        assert alerts_view.settings_of(alerts_view.draft_of(s)) == s
    d = alerts_view.draft_of(Settings(pregame=[], max_legs=None))
    assert d["pregame_on"] is False and d["pregame"] == [[50, 100], [500, 20], [1000, 10]] and d["max_legs"] == 5
    assert alerts_view.everything_on(d).max_legs == 5 and alerts_view.everything_on(d).pregame == []


def test_preview_says_what_each_filter_leaves_out():
    rows = bets()
    s = Settings()
    view = alerts_view.preview(rows, s)
    assert view["rows"] == 7 and view["cleared"] == 6
    assert [r["ticker"] for r, _ in view["alert"]] == ["KXMVECOMBO-GOOD"]
    assert {r["ticker"]: why for r, why in view["dropped"]} == {
        "KXMVECOMBO-THIN": ["thin"], "KXMVECOMBO-SAME": ["same_game"], "KXMVECOMBO-SIX": ["legs"],
        "KXMVECOMBO-LIVE4": ["legs_live"], "KXMVECOMBO-3LG": ["leagues"]}
    loose = Settings(skip_same_game=False, max_legs_live=None)
    d = alerts_view.draft_of(loose)
    view = alerts_view.preview(rows, loose, alerts_view.everything_on(d))
    assert {r["ticker"] for r, _ in view["alert"]} == {"KXMVECOMBO-GOOD", "KXMVECOMBO-SAME", "KXMVECOMBO-LIVE4"}
    assert view["catches"]["same_game"] == 1 and view["catches"]["legs_live"] == 1  # what they'd catch switched on
    html = alerts_view._bet_html(view["alert"][0][0], ["thin"], "mid")
    assert "quote?t=KXMVECOMBO-" in html and "thin leg book" in html


def page(**state):
    at = AppTest.from_file(APP, default_timeout=60)
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def test_alerts_tool_saves_what_you_switch():
    at = page()
    assert not at.exception
    save = next(b for b in at.button if b.label == "Save")
    assert save.disabled  # nothing changed yet
    at.toggle(key="al_skip_same_game_1").set_value(False).run()
    assert at.session_state["al_draft"]["skip_same_game"] is False
    next(b for b in at.button if b.label == "Save").click().run()
    saved = at.session_state["_store"].data
    assert saved["skip_same_game"] is False and saved["max_legs"] == 5 and saved["saved_at"]
    assert Settings.from_dict(saved) == Settings(skip_same_game=False)
    assert next(b for b in at.button if b.label == "Save").disabled  # saved: nothing left to save


def test_alerts_tool_tiers_add_remove_and_undo():
    at = page()
    next(b for b in at.button if b.label == "＋ Add a live tier").click().run()
    assert at.session_state["al_draft"]["live"] == [[1000, 100], [5000, 50], [10000, 50.0]]
    at.number_input(key="al_live2e_2").set_value(25.0).run()
    assert alerts_view.settings_of(at.session_state["al_draft"]).live == [(1000, 1.0), (5000, 0.5), (10000, 0.25)]
    next(b for b in at.button if b.key == "al_live0x_2").click().run()
    assert at.session_state["al_draft"]["live"] == [[5000, 50], [10000, 25.0]]
    next(b for b in at.button if b.label == "Undo changes").click().run()
    assert alerts_view.settings_of(at.session_state["al_draft"]) == Settings()
    assert not at.exception


def test_alerts_tool_wont_save_without_permission():
    at = page(_can_save=False, _note="Unlock with your TRADE_PIN to save.")
    at.toggle(key="al_skip_thin_1").set_value(False).run()
    assert next(b for b in at.button if b.label == "Save").disabled
    assert any("TRADE_PIN" in c.value for c in at.caption)
    assert at.session_state["_store"].data is None


def test_memory_file_hands_out_copies():
    m = MemoryFile()
    m.write({"a": [1]}, "x")
    data, sha = m.read()
    data["a"].append(2)
    assert m.read() == ({"a": [1]}, "1")
