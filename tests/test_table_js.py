"""The table copies bet cards and builds Kalshi links in the browser, so its JS must match the Python."""

import json
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
    return subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                          check=True).stdout


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
    assert out.endswith("EV CRX +3568.4% (fair -363) · 19u")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_bet_link_matches_python(tmp_path):
    ticker = "KXMVECROSSCATEGORY-S2026L76DQIJNMS46XWZS-NRA3G3JTU"
    assert run_js(tmp_path, "betUrl", f"betUrl({json.dumps(ticker)})") == bet_url(ticker)
    assert bet_url(ticker) == f"https://kalshi.com/markets_by_ticker/{ticker}"
