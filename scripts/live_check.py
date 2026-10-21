"""Smoke test against the live Kalshi API: pull recent combo trades and score a few.

python scripts/live_check.py [lookback_minutes] [min_stake]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker.client import KalshiClient  # noqa: E402
from combo_tracker.format import bet_card  # noqa: E402
from combo_tracker.pipeline import refresh  # noqa: E402

lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 15
min_stake = float(sys.argv[2]) if len(sys.argv) > 2 else 100

t0 = time.time()
snap = refresh(KalshiClient(), lookback_minutes=lookback, min_stake=min_stake)
print(f"{time.time() - t0:.1f}s · {snap.combo_trades_seen} combo fills · {snap.bets_seen} bets · "
      f"{snap.bets_scored} scored ≥ ${min_stake:.0f} · {len(snap.errors)} errors")
for e in snap.errors[:5]:
    print("  error:", e)
rows = sorted(snap.rows, key=lambda r: r["ev_pct_mid"] if r["ev_pct_mid"] is not None else -9, reverse=True)
for r in rows[:5]:
    print()
    print(bet_card(r))
    print(f"(${r['stake']:,.0f} bet · {r['contracts']:,.0f} contracts · net EV ${r['ev_net'] or 0:,.2f} · "
          f"min depth {r['min_depth_ratio']} · {r['ticker']})")
