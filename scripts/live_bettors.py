"""Run one small pass of the Bettors tracker against live Kalshi data and print the table.

Usage: python scripts/live_bettors.py [top_n] [max_fills]
"""

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

from combo_tracker.bettors import BettorTracker  # noqa: E402
from combo_tracker.client import KalshiClient  # noqa: E402


def fmt(x, d=0):
    return "-" if x is None else f"{x:,.{d}f}"


def main() -> None:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    max_fills = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    client = KalshiClient(rate_per_sec=8)
    t = BettorTracker(client, top_n=top_n, max_fills=max_fills, first_pages=8)
    start = time.time()
    t.read_feed()
    print(f"feed: {time.time() - start:.1f}s, requests so far {client.requests_made}")
    for name in sys.argv[3:]:
        t.add(name)
    stats = t.feed_stats()
    print(f"tennis feed: {t.feed_trades} trades, {t.feed_named} with a name, {len(stats)} nicknames")
    s = time.time()
    t.analyze(t.candidates())
    t.publish()
    print(f"analyze: {time.time() - s:.1f}s, total {time.time() - start:.1f}s, requests {client.requests_made}")
    print("errors:", t.snapshot.errors)
    rows = sorted(t.snapshot.rows, key=lambda r: -(r["tennis"]["z"] if r["tennis"]["z"] is not None else -99))
    print(f"\n{'nickname':24} {'src':18} {'hid':5} {'PnL all':>12} {'PnL 30d':>10} {'mkts':>7} | "
          f"{'n':>5} {'cov':>4} {'vol':>9} {'EV':>8} {'EV%':>6} {'set':>4} {'PnL':>8} {'edge':>8} {'z':>5} {'tk%':>4}")
    for r in rows:
        a = r["tennis"]
        print(f"{r['nick'][:24]:24} {','.join(r['src'])[:18]:18} {str(r['hidden'])[:5]:5} {fmt(r['pnl_all']):>12} "
              f"{fmt(r['pnl_30d']):>10} {fmt(r['markets_all']):>7} | {a['n']:>5} {fmt((a['ev_cover'] or 0) * 100):>4} "
              f"{fmt(a['vol']):>9} {fmt(a['ev']):>8} {fmt((a['ev_roi'] or 0) * 100, 1):>6} {a['settled']:>4} "
              f"{fmt(a['pnl']):>8} {fmt(a['edge']):>8} {fmt(a['z'], 2):>5} {fmt((a['taker_share'] or 0) * 100):>4}")
    sample = next((r for r in rows if r["recent"]), None)
    if sample:
        print(f"\nlatest trades for {sample['nick']}:")
        for f in sample["recent"][:8]:
            print(" ", f["tk"], f["side"], f"{f['price']:.2f}", f["n"], "taker" if f["taker"] else "maker",
                  "fair", fmt(f["fair"], 3), "ev", fmt(f["ev"], 2), "pnl", fmt(f["pnl"], 2))


if __name__ == "__main__":
    main()
