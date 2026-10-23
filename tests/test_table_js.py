"""The table copies bet cards and builds Kalshi links in the browser, so its JS must match the Python."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from combo_tracker.engine import kelly_fraction
from combo_tracker.format import bet_card
from combo_tracker.links import bet_url

TABLE_JS = Path(__file__).parents[1] / "combo_tracker" / "ui" / "table.js"


def run_js(tmp_path, names: str, expr: str) -> str:
    """Import `names` from table.js and print `expr`."""
    module = tmp_path / "table.mjs"
    module.write_text(TABLE_JS.read_text())
    script = f"import {{ {names} }} from {json.dumps(module.as_uri())};process.stdout.write({expr});"
    env = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}  # toLocaleString: "+2,460%"
    return subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                          check=True, env=env).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_bet_card_matches_python(tmp_path):
    price, mid, crx = 100 / 4679, 383 / 483, 363 / 463
    legs = [("no", "Mookie Betts: 2+", "Milwaukee vs Los Angeles D: Home Runs"),
            ("no", "Freddy Peralta: 6+", "Cleveland vs Tampa Bay: Strikeouts")]
    row = {"side": "yes", "price": price, "fair_mid": mid, "fair_crx": crx,
           "ev_pct_mid": mid / price - 1, "ev_pct_crx": crx / price - 1,
           "kelly_mid": kelly_fraction(mid, price), "kelly_crx": kelly_fraction(crx, price),
           "leg_detail": [{"side": s, "leg": n, "event_title": e} for s, n, e in legs]}
    slim = {"side": "yes", "price": price, "fm": mid, "fc": crx, "evm": row["ev_pct_mid"], "evc": row["ev_pct_crx"],
            "km": row["kelly_mid"], "kc": row["kelly_crx"], "legs": [{"s": s, "n": n, "e": e} for s, n, e in legs]}

    out = run_js(tmp_path, "betCard", f"betCard({{kelly: 0.5, unitPct: 2}}, {json.dumps(slim)})")
    assert out == bet_card(row, kelly_mult=0.5, unit_pct=2)
    assert out.endswith("EV CRX +3568.4% (fair -363 / 1.28x) · 19u")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_bet_link_matches_python(tmp_path):
    ticker = "KXMVECROSSCATEGORY-S2026L76DQIJNMS46XWZS-NRA3G3JTU"
    assert run_js(tmp_path, "betUrl", f"betUrl({json.dumps(ticker)})") == bet_url(ticker)
    assert bet_url(ticker) == f"https://kalshi.com/markets_by_ticker/{ticker}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_ev_colors_match_home(tmp_path):
    """A bet's EV looks the same on Combos as on Home: same scale, same colors."""
    home = tmp_path / "home.mjs"
    home.write_text((TABLE_JS.parent / "home.js").read_text())
    values = [0, 0.001, 0.05, 0.0999, 0.1, 0.1996, 0.2, 0.25, 0.65, 1, 1.996, 2, 3.5, 4.996, 5, 8, 20]
    expr = (f"JSON.stringify({json.dumps(values)}.map((x) => [evHeat(x), evStyle(x)]))")
    table = json.loads(run_js(tmp_path, "evHeat, evStyle", expr))
    script = (f"import {{ evHeat, evStyle }} from {json.dumps(home.as_uri())};"
              f"process.stdout.write({expr});")
    assert table == json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True,
                                              text=True, check=True).stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_ev_tiers_and_text(tmp_path):
    values = [None, -0.08, 0, 0.0004, 0.001, 0.09, 0.1, 0.99, 1, 1.8, 2.2, 4.99, 5, 8.2]
    tiers = json.loads(run_js(tmp_path, "evTier", f"JSON.stringify({json.dumps(values)}.map(evTier))"))
    assert tiers == ["none", "neg", "zero", "zero", "t0", "t0", "t1", "t1", "t2", "t2", "t3", "t3", "t4", "t4"]
    values = [0.016, -0.084, 0.1, 8.2, 24.6, -0.0004, None]
    text = json.loads(run_js(tmp_path, "pctShort", f"JSON.stringify({json.dumps(values)}.map(pctShort))"))
    assert text == ["+1.6%", "-8.4%", "+10%", "+820%", "+2,460%", "+0.0%", "–"]  # the same text as Home's badge


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_pills_that_read_the_same_look_the_same(tmp_path):
    """Colors and tiers go by the EV as printed, so "+20%" is never two different greens."""
    pairs = [[0.1996, 0.2], [0.0996, 0.1], [1.996, 2], [4.996, 5], [-0.0004, 0]]
    expr = (f"JSON.stringify({json.dumps(pairs)}.map(([a, b]) => [pctShort(a) === pctShort(b), "
            "evStyle(a) === evStyle(b), evTier(a) === evTier(b)]))")
    assert json.loads(run_js(tmp_path, "evStyle, evTier, pctShort", expr)) == [[True, True, True]] * len(pairs)
    assert json.loads(run_js(tmp_path, "evShown", "JSON.stringify([0.0994, 0.1049, 0.21, 8.204].map(evShown))")) == \
        [0.099, 0.1, 0.21, 8.2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_times_read_as_clock_times(tmp_path):
    """Bets show the clock time they were placed, and pre-match bets when their first game starts."""
    now = "new Date(2026, 9, 23, 12, 0).getTime()"  # a Friday, noon local time
    expr = (f"JSON.stringify([[-1, 0.5, 25.5, 160.5, 600, 1500].map((m) => startsIn({now} + m * 60000, {now})), "
            f"[clock({now} - 4 * 60000, {now}), clock({now} - 13 * 3600000, {now})]])")
    starts, clocks = json.loads(run_js(tmp_path, "clock, startsIn", expr).replace("\u202f", " "))
    assert starts == ["starting", "starts in 1m", "starts in 25m", "starts 2:40 PM", "starts 10:00 PM", "Sat 1:00 PM"]
    assert clocks == ["11:56 AM", "Thu 11:00 PM"]


ROWS = [  # what the filters and sorts look at, for a handful of bets
    {"id": "a", "t": 5, "stake": 50, "evm": 0.30, "evc": 0.10, "km": 0.02, "kc": 0.01, "sg": 0, "live": True, "hay": "nba"},
    {"id": "b", "t": 4, "stake": 900, "evm": -0.05, "evc": -0.10, "km": 0, "kc": 0, "sg": 1, "live": False, "hay": "mlb"},
    {"id": "c", "t": 3, "stake": 20, "evm": None, "evc": None, "km": None, "kc": None, "sg": 0, "live": False, "hay": "atp"},
    {"id": "d", "t": 2, "stake": 300, "evm": 2.5, "evc": 1.9, "km": 0.08, "kc": 0.06, "sg": 2, "live": True, "hay": "nfl"},
    {"id": "e", "t": 1, "stake": 10, "evm": 0.05, "evc": -0.02, "km": 0.004, "kc": 0, "sg": 0, "live": False, "hay": "nba"},
]
for _r in ROWS:
    _r["legs"] = [{"sp": 0.01, "z": 500}]
ROWS[4]["legs"] = [{"sp": 0.08, "z": 500}]  # thin: spread over 3¢

UI = {"minEv": 0, "minQk": None, "indep": False, "basis": "mid", "phase": None, "q": "", "hideThin": False,
      "thinSpread": 3, "thinSize": 50, "sort": "time", "dir": -1, "kelly": 0.25, "unitPct": 1}


def shown(tmp_path, **ui) -> list[str]:
    expr = f"JSON.stringify(visibleRows({json.dumps({**UI, **ui})}, {{rows: {json.dumps(ROWS)}}}).map((r) => r.id))"
    return json.loads(run_js(tmp_path, "visibleRows", expr))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_filters(tmp_path):
    assert shown(tmp_path) == ["a", "d", "e"]  # Min EV 0%: +EV only
    assert shown(tmp_path, minEv=None) == ["a", "b", "c", "d", "e"]  # Any: negative and unpriced too
    assert shown(tmp_path, minEv=10, basis="cross") == ["a", "d"]
    assert shown(tmp_path, minQk=0.5) == ["a", "d"]  # ¼ Kelly, 1u = 1%: a is 0.5u, e only 0.1u
    assert shown(tmp_path, indep=True) == ["a", "e"]
    assert shown(tmp_path, hideThin=True) == ["a", "d"]
    assert shown(tmp_path, phase="live") == ["a", "d"] and shown(tmp_path, phase="pre") == ["e"]
    assert shown(tmp_path, q="nba") == ["a", "e"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_search_matches_league_and_sport(tmp_path):
    """The search box finds a bet by a leg's league or sport, whose names arrive in data.strs."""
    data = {"strs": ["Sinner", "Sinner vs Alcaraz", "ATP", "tennis"],
            "rows": [{**ROWS[0], "ticker": "T1", "side": "yes", "legs": [{"n": 0, "e": 1, "lg": 2, "spt": 3}]},
                     {**ROWS[3], "ticker": "T2", "side": "no", "legs": [{"n": "Chiefs", "e": "KC vs BUF"}]}]}
    for r in data["rows"]:
        del r["hay"], r["id"]
    expr = (f"JSON.stringify(['tennis', 'atp', 'SINNER', 'chiefs'].map((q) => visibleRows("
            f"{{...{json.dumps(UI)}, q}}, hydrate({json.dumps(data)})).map((r) => r.id)))")
    assert json.loads(run_js(tmp_path, "hydrate, visibleRows", expr)) == [["T1|yes"], ["T1|yes"], ["T1|yes"], ["T2|no"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_sorts_put_missing_ev_last(tmp_path):
    assert shown(tmp_path, minEv=None, sort="ev") == ["d", "a", "e", "b", "c"]
    assert shown(tmp_path, minEv=None, sort="ev", dir=1) == ["b", "e", "a", "d", "c"]
    assert shown(tmp_path, minEv=None, sort="qk") == ["d", "a", "e", "b", "c"]
    assert shown(tmp_path, minEv=None, sort="qk", dir=1) == ["b", "e", "a", "d", "c"]
    assert shown(tmp_path, minEv=None, sort="stake") == ["b", "d", "a", "c", "e"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_toggle_counts_match_what_they_show(tmp_path):
    expr = f"JSON.stringify(counts({json.dumps({**UI, 'indep': True})}, {{rows: {json.dumps(ROWS)}}}))"
    n = json.loads(run_js(tmp_path, "counts", expr))
    assert n == {"all": 2, "live": 1, "pre": 1, "indep": 2, "thin": 1}
    assert len(shown(tmp_path, indep=True, phase="live")) == n["live"]
    assert len(shown(tmp_path, indep=True, hideThin=True)) == n["thin"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_old_saved_settings_still_load(tmp_path):
    saved = {"sort": "om", "minEv": 5, "minQk": 0.5, "basis": "cross", "q": "kept?", "open": "x"}
    expr = (f"(globalThis.localStorage = {{getItem: () => {json.dumps(json.dumps(saved))}}}, "
            "JSON.stringify(loadUi()))")
    S = json.loads(run_js(tmp_path, "loadUi", expr))
    assert (S["sort"], S["minEv"], S["minQk"], S["basis"], S["q"], S["open"]) == ("time", 5, 0.5, "cross", "", None)
