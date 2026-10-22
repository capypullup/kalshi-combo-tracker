"""Smoke test against the live Kalshi API: pull recent combo trades and score a few.

python scripts/live_check.py [lookback_minutes] [min_stake]
"""

import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker.client import COMBO_PREFIX, KalshiClient  # noqa: E402
from combo_tracker.format import bet_card  # noqa: E402
from combo_tracker.pipeline import refresh  # noqa: E402

lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 15
min_stake = float(sys.argv[2]) if len(sys.argv) > 2 else 100
client = KalshiClient()


def stage(name, fn):
    t0 = time.time()
    try:
        out = fn()
        print(f"[ok] {name} ({time.time() - t0:.1f}s)")
        return out
    except Exception as exc:  # report and keep going so one run shows every problem
        print(f"[FAIL] {name} ({time.time() - t0:.1f}s): {exc!r}")
        traceback.print_exc(limit=3, file=sys.stdout)
        return None


# Stage by stage first, so a failure says exactly which call broke.
since = datetime.now(timezone.utc) - timedelta(minutes=lookback)
page = stage("one trades page", lambda: client._get("/markets/trades", {"limit": 1000, "min_ts": int(since.timestamp())}))
if page:
    trades = page.get("trades", [])
    combos = [t for t in trades if t["ticker"].startswith(COMBO_PREFIX)]
    span = f"{trades[-1]['created_time']} .. {trades[0]['created_time']}" if trades else "empty"
    print(f"     {len(trades)} trades on page 1, {len(combos)} combo, span {span}, cursor={'yes' if page.get('cursor') else 'no'}")
    print(f"     sample trade: {trades[0] if trades else None}")
    if combos:
        m = stage("combo market", lambda: client.get_markets([combos[0]["ticker"]]))
        if m:
            market = next(iter(m.values()))
            print(f"     legs: {market.get('mve_selected_legs')}")
            legs = market.get("mve_selected_legs") or []
            if legs:
                stage("leg markets", lambda: client.get_markets([l["market_ticker"] for l in legs]))
                ev = stage("leg events", lambda: client.get_events([l["event_ticker"] for l in legs]))
                print(f"     event titles: {[e.get('title') for e in (ev or {}).values()]}")
                ob = stage("leg orderbooks", lambda: client.get_orderbooks([l["market_ticker"] for l in legs]))
                if ob:
                    first = next(iter(ob.values()))
                    print(f"     orderbook keys: {list(first)}; yes levels: {str(first.get('yes_dollars'))[:200]}")

print()
t0 = time.time()
snap = stage(f"full refresh ({lookback} min, bets ≥ ${min_stake:.0f})",
             lambda: refresh(client, lookback_minutes=lookback, min_stake=min_stake))
if snap is None:
    sys.exit(1)
print(f"{time.time() - t0:.1f}s · {snap.combo_trades_seen} combo fills · {snap.bets_seen} bets · "
      f"{snap.bets_scored} scored · {len(snap.errors)} errors")
for e in snap.errors[:5]:
    print("  error:", e)
rows = sorted(snap.rows, key=lambda r: r["ev_pct_mid"] if r["ev_pct_mid"] is not None else -9, reverse=True)
for r in rows[:8]:
    print()
    print(bet_card(r))
    print(f"(${r['stake']:,.0f} bet · {r['contracts']:,.0f} contracts · net EV ${r['ev_net'] or 0:,.2f} · "
          f"min depth {r['min_depth_ratio']} · flags SG={r['same_game_legs']} missing={r['missing_quotes']} · {r['ticker']})")
