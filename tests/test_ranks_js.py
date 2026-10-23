"""The ranked-bettors and live-bets tables filter and format in the browser; check their JS with node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from combo_tracker.demo import DemoClient, demo_store
from combo_tracker.recorder import build_view, pack_live, pack_view

UI = Path(__file__).parents[1] / "combo_tracker" / "ui"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_js(tmp_path, file: str, names: str, expr: str) -> str:
    module = tmp_path / file.replace(".js", ".mjs")
    module.write_text((UI / file).read_text())
    script = f"import {{ {names} }} from {json.dumps(module.as_uri())};process.stdout.write({expr});"
    return subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout


def test_ranks_filters_whales_and_tennis(tmp_path):
    view = build_view(demo_store(DemoClient()))
    whales = pack_view(view, "a:all")
    S = {"size": 10000, "period": "all", "locks": False, "minN": None, "only": "all", "sort": "vol", "dir": -1, "q": ""}
    out = json.loads(run_js(tmp_path, "ranks.js", "visibleRows",
                            f"JSON.stringify(visibleRows('whales', {json.dumps(S)}, {json.dumps(whales)}).map((r) => [r.code.slice(0, 6), r.s.vol]))"))
    assert out and all(r["w"] for r in whales["rows"] if r["code"][:6] in {c for c, _ in out})
    assert [v for _, v in out] == sorted((v for _, v in out), reverse=True)
    tennis = pack_view(view, "t:30")
    S2 = {**S, "size": None, "period": "30", "minN": 5, "sort": "z", "only": "tagged"}
    tagged = json.loads(run_js(tmp_path, "ranks.js", "visibleRows",
                               f"JSON.stringify(visibleRows('tennis', {json.dumps(S2)}, {json.dumps(tennis)}).map((r) => r.code))"))
    assert tagged == [r["code"] for r in tennis["rows"] if r["tg"] and r["s"]["n"] >= 5]
    S3 = {**S2, "only": "all", "q": "tennis.sh"}
    named = json.loads(run_js(tmp_path, "ranks.js", "visibleRows",
                              f"JSON.stringify(visibleRows('tennis', {json.dumps(S3)}, {json.dumps(tennis)}).map((r) => r.name))"))
    assert named == ["tennis.sharp"]


def test_ranks_formats(tmp_path):
    out = run_js(tmp_path, "ranks.js", "money0, odds, variantOf",
                 "JSON.stringify([money0(15109), money0(-2500), money0(999), money0(12000000), odds(0.305), odds(0.997), "
                 "variantOf('whales', {locks: true, period: '7'}), variantOf('tennis', {locks: true, period: '30'})])")
    assert json.loads(out) == ["+$15k", "-$2.5k", "+$999", "+$12.0M", "+228", "-33233", "a+:7", "t:30"]


def test_live_filters(tmp_path):
    view = build_view(demo_store(DemoClient()))
    live = pack_live(view)
    live["live"] = live["live"] + [{"id": "p1", "t": view["saved"], "tk": "KXATPMATCH-X-A", "s": "yes", "n": 10, "p": 0.5,
                                    "fm": 0.55, "ti": "A beats B", "r": None, "c": None, "name": "pro.player", "tg": False}]
    for who, check in (("whales", "every((b) => b.w)"), ("profiles", "every((b) => !b.c)"), ("tagged", "every((b) => b.tg)")):
        S = {"who": who, "min": 0, "q": "", "show": 40}
        out = json.loads(run_js(tmp_path, "live.js", "visibleBets",
                                f"JSON.stringify([visibleBets({json.dumps(S)}, {json.dumps(live)}).length, visibleBets({json.dumps(S)}, {json.dumps(live)}).{check}])"))
        assert out[1] is True and (out[0] > 0 or who == "tagged"), who
    S = {"who": "all", "min": 5000, "q": "", "show": 40}
    out = json.loads(run_js(tmp_path, "live.js", "visibleBets", f"JSON.stringify(visibleBets({json.dumps(S)}, {json.dumps(live)}).every((b) => b.n * b.p >= 5000))"))
    assert out is True
