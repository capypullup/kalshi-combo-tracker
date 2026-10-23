"""One-off probe: which series do combo legs come from, and what sport/league is each? Runs in CI only."""

import collections
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from combo_tracker.client import COMBO_PREFIX, KalshiClient  # noqa: E402

client = KalshiClient()
print("========== all series ==========")
series = {}
try:
    cursor = None
    for _ in range(30):
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = client._get("/series", params)
        for s in data.get("series", []):
            series[s["ticker"]] = s
        cursor = data.get("cursor")
        if not cursor:
            break
    print(len(series), "series; sample keys:", sorted(next(iter(series.values())).keys()) if series else None)
except Exception:
    traceback.print_exc(file=sys.stdout)

print("========== series in recent combo legs ==========")
counts = collections.Counter()
try:
    tickers, cursor = [], None
    for _ in range(8):
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = client._get("/markets/trades", params)
        tickers += [t["ticker"] for t in page.get("trades", []) if t["ticker"].startswith(COMBO_PREFIX)]
        cursor = page.get("cursor")
        if not cursor:
            break
    combos = list(dict.fromkeys(tickers))[:400]
    markets = client.get_markets(combos, batch_size=100)
    for m in markets.values():
        for leg in m.get("mve_selected_legs") or []:
            counts[leg["event_ticker"].split("-")[0]] += 1
    print(len(combos), "combos,", sum(counts.values()), "legs,", len(counts), "series")
except Exception:
    traceback.print_exc(file=sys.stdout)

for tk, n in counts.most_common():
    s = series.get(tk)
    if s is None:
        try:
            s = client._get(f"/series/{tk}").get("series", {})
        except Exception as exc:
            s = {"error": repr(exc)[:80]}
    print(json.dumps({"t": tk, "n": n, "title": s.get("title"), "cat": s.get("category"), "tags": s.get("tags")}))

print("========== every Sports series (ticker | title | tags) ==========")
for tk, s in sorted(series.items()):
    if (s.get("category") or "").lower() == "sports" or tk in counts:
        print(f"{tk} | {s.get('title')} | {s.get('tags')}")
print("========== other categories (counts) ==========")
print(collections.Counter(s.get("category") for s in series.values()).most_common())
