"""Probe: run the QUOTE page's live check on the busiest real combos right now (public data only)."""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from combo_tracker.client import KalshiClient  # noqa: E402
from combo_tracker.pipeline import GameStarts  # noqa: E402
from combo_tracker.quote import check, odds  # noqa: E402

c = KalshiClient(rate_per_sec=6)
trades, cursor = [], None
for _ in range(8):
    d = c._get("/markets/trades", {"limit": 1000, **({"cursor": cursor} if cursor else {})})
    trades += [t for t in d.get("trades", []) if t["ticker"].startswith("KXMVE")]
    cursor = d.get("cursor")
starts = GameStarts(c)
starts.refresh()
busy = [t for t, _ in Counter(t["ticker"] for t in trades).most_common(6)]
lone = [t["ticker"] for t in trades if t["ticker"] not in busy][:4]
for ticker in busy + lone:
    t = next(x for x in trades if x["ticker"] == ticker)
    side = t.get("taker_outcome_side") or t["taker_side"]
    price = float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])
    t0 = time.time()
    try:
        r = check(c, ticker, side, price, float(t["count_fp"]), starts)
    except Exception as e:
        print(ticker, "ERR", repr(e)[:300])
        continue
    print(f"\n{ticker} {side} @ {price:.4f} ({odds(price)}) legs={len(r.scored['leg_detail'])} took {time.time() - t0:.1f}s")
    print(f"  {r.verdict}: {r.reason}")
    print(f"  book ask {r.book_ask} x{r.book_ask_size:.0f}, at price {r.book_at_price:.0f}; best now {r.best_now}; "
          f"EV now {r.ev_now}; EV at price mid {r.scored['ev_pct_mid']} crx {r.scored['ev_pct_crx']}")
    print("  fills:", [(f.ts.strftime('%H:%M:%S'), f.side, f.price, f.contracts) for f in r.fills[:6]])
