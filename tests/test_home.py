"""Home picks the +EV bets, best first, and its JS builds the same Kalshi links as Python."""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from combo_tracker.home import pack_home, picks
from combo_tracker.links import bet_url

HOME_JS = Path(__file__).parents[1] / "combo_tracker" / "ui" / "home.js"


def row(ticker, evm, evc, missing=0):
    leg = {"side": "yes", "leg": "BOS wins", "event_title": "Los Angeles L vs Boston: Winner",
           "ticker": "KXNBAGAME-26OCT21LALBOS-BOS", "status": "active", "hours_to_start": 2.0,
           "start": datetime(2026, 10, 23, 23, tzinfo=timezone.utc)}
    return {"ticker": ticker, "side": "yes", "last_time": datetime(2026, 10, 23, 3, tzinfo=timezone.utc),
            "price": 0.2, "contracts": 1000, "stake": 200, "ev_pct_mid": evm, "ev_pct_crx": evc,
            "fair_mid": 0.2 * (1 + evm) if evm is not None else None, "fair_crx": None, "legs": 2, "games": 2,
            "same_game_legs": 0, "missing_quotes": missing, "inactive_legs": 0, "outside_band": True,
            "leg_detail": [leg, {**leg, "ticker": "KXMLBGAME-26OCT21NYYLAD-NYY"}]}


def test_picks_keeps_quoted_positive_ev_best_cross_first():
    rows = [row("A", 0.3, 0.1), row("B", -0.1, -0.2), row("C", 0.5, 0.4), row("D", 2.0, 1.0, missing=1),
            row("E", 0.05, None), row("F", None, None)]
    assert [r["ticker"] for r in picks(rows)] == ["C", "A", "E"]


def test_card_has_leagues_and_first_start():
    card = pack_home([row("A", 0.3, 0.1)])[0]
    assert [(l["lg"], l["spt"]) for l in card["legs"]] == [("NBA", "basketball"), ("MLB", "baseball")]
    assert card["start"] == int(datetime(2026, 10, 23, 23, tzinfo=timezone.utc).timestamp() * 1000)
    json.dumps(card)  # the component gets it as JSON


def run_js(tmp_path, expr: str) -> str:
    module = tmp_path / "home.mjs"
    module.write_text(HOME_JS.read_text())
    script = f"import * as H from {json.dumps(module.as_uri())};process.stdout.write(String({expr}));"
    return subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                          check=True).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_ev_badge_scale(tmp_path):
    heats = json.loads(run_js(tmp_path, "JSON.stringify([0.01, 0.1, 1, 8, 20].map(H.evHeat))"))
    assert heats == sorted(heats) and heats[-1] == 1 and heats[-2] == 1
    assert run_js(tmp_path, "H.evText(8)") == "+800%"
    assert "rgba(" in run_js(tmp_path, "H.evStyle(0.05)")  # under +10%: a pale tint
    assert "hsl(" in run_js(tmp_path, "H.evStyle(0.1)")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_bet_link_matches_python(tmp_path):
    ticker = "KXMVECROSSCATEGORY-S2026ABC-123"
    assert run_js(tmp_path, f"H.betUrl({json.dumps(ticker)})") == bet_url(ticker)
