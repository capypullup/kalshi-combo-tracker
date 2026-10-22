"""Smoke test against the live Kalshi API: pull recent combo trades and score a few.

python scripts/live_check.py [lookback_minutes] [min_stake]
"""

import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker.client import COMBO_PREFIX, KalshiClient  # noqa: E402
from combo_tracker.format import bet_card  # noqa: E402
from combo_tracker.view import is_live, pack, slim  # noqa: E402
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.pipeline import Board, Scorer  # noqa: E402

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
                lm = stage("leg markets", lambda: client.get_markets([l["market_ticker"] for l in legs]))
                if lm:
                    one = next(iter(lm.values()))
                    print("     leg market fields:", {k: v for k, v in one.items() if k not in ("rules_primary", "rules_secondary")})
                ev = stage("leg events", lambda: client.get_events([l["event_ticker"] for l in legs]))
                print(f"     event titles: {[e.get('title') for e in (ev or {}).values()]}")
                if ev:
                    print("     event fields:", next(iter(ev.values())))
                ob = stage("leg orderbooks", lambda: client.get_orderbooks([l["market_ticker"] for l in legs]))
                if ob:
                    first = next(iter(ob.values()))
                    print(f"     orderbook keys: {list(first)}; yes levels: {str(first.get('yes_dollars'))[:200]}")

print()
col = Collector(client, max_window_minutes=lookback, initial_backfill_minutes=lookback)
stage(f"collector backfill ({lookback} min)", col.poll)
print(f"     scanned {col.trades_scanned:,} trades, kept {len(col.trades):,} combo fills, "
      f"{client.requests_made} requests so far")
reqs = client.requests_made
stage("collector incremental poll", col.poll)
print(f"     incremental poll used {client.requests_made - reqs} requests")

scorer = Scorer(client)
reqs = client.requests_made
snap = stage(f"score bets ≥ ${min_stake:.0f}", lambda: scorer.score(col.window(lookback), min_stake=min_stake))
if snap is None:
    sys.exit(1)
print(f"     {client.requests_made - reqs} requests · batch orderbooks ok: {client.batch_orderbooks_ok}")
print(f"     {snap.combo_trades_seen} combo fills · {snap.bets_seen} bets · {snap.bets_over_min} ≥ ${min_stake:.0f} · "
      f"{snap.bets_scored} scored · {len(snap.errors)} errors")
reqs = client.requests_made
stage("score again (warm caches)", lambda: scorer.score(col.window(lookback), min_stake=min_stake))
print(f"     {client.requests_made - reqs} requests")
board = Board(scorer, col.window, default_view=(lookback, min_stake))
stage("background pass (Board.refresh)", lambda: board.refresh(force=True))
t0 = time.time()
board.get(lookback, min_stake)
print(f"     a page read after that took {(time.time() - t0) * 1000:.0f} ms")
if snap.rows:
    reqs = client.requests_made
    deep = stage("open one card (full depth)", lambda: scorer.rescore_with_depth(snap.rows[0], col.window(lookback)))
    print(f"     {client.requests_made - reqs} requests · top-of-book EV {snap.rows[0]['ev_pct_mid']} -> "
          f"full-depth EV {deep and deep['ev_pct_mid']}")
for e in snap.errors[:5]:
    print("  error:", e)
legs = [l for r in snap.rows for l in r["leg_detail"]]
if legs:
    def pctl(xs, q):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))]
    spreads = [l["spread"] * 100 for l in legs if l["spread"] is not None]
    sizes = [min(l["top_bid_size"], l["top_ask_size"]) for l in legs]
    print(f"     leg spreads (¢) p25/50/75/90: {[round(pctl(spreads, q), 1) for q in (.25, .5, .75, .9)]} · "
          f"no two-sided quote: {len(legs) - len(spreads)}/{len(legs)}")
    print(f"     smaller top-of-book size (contracts) p10/25/50/75: {[round(pctl(sizes, q)) for q in (.1, .25, .5, .75)]}")
    for cut_sp, cut_sz in [(2, 50), (3, 100), (4, 100), (5, 100), (4, 250), (10, 0)]:
        thin = sum(1 for r in snap.rows if any(l["spread"] is None or l["spread"] * 100 > cut_sp + 1e-9 or
                                               min(l["top_bid_size"], l["top_ask_size"]) < cut_sz for l in r["leg_detail"]))
        print(f"     bets with a leg spread > {cut_sp}¢ or top size < {cut_sz}: {thin}/{len(snap.rows)}")
    known = [l for l in legs if l["start"] is not None]
    print(f"     legs with a scheduled start: {len(known)}/{len(legs)} · started: {sum(1 for l in known if l['hours_to_start'] <= 0)} · "
          f"schedule: {len(scorer.starts.by_event)} events, error {scorer.starts.error}")
    missing = sorted({l["ticker"].split("-")[0] for l in legs if l["start"] is None})
    print(f"     series with no scheduled start: {missing[:40]}")
    old_rule = sum(1 for r in snap.rows if r["inactive_legs"] or (r["hours_to_first_leg"] is not None and r["hours_to_first_leg"] < 3))
    print(f"     live bets: {sum(1 for r in snap.rows if is_live(r))}/{len(snap.rows)} (old 3h rule said {old_rule})")
payload = json.dumps(pack(snap.rows), allow_nan=False)  # what the phone table receives
print(f"     table payload: {len(payload) / 1024:.0f} KB packed "
      f"({len(json.dumps([slim(r) for r in snap.rows])) / 1024:.0f} KB unpacked) for {len(snap.rows)} rows · "
      f"{sum(1 for r in snap.rows if slim(r)['live'])} look live")
missing = sum(1 for r in snap.rows if r["missing_quotes"])
print(f"     rows with a missing leg quote: {missing}/{len(snap.rows)}")
rows = sorted(snap.rows, key=lambda r: r["ev_pct_mid"] if r["ev_pct_mid"] is not None else -9, reverse=True)
for r in rows[:8]:
    print()
    print(bet_card(r))
    print(f"(${r['stake']:,.0f} bet · {r['contracts']:,.0f} contracts · net EV ${r['ev_net'] or 0:,.2f} · "
          f"min depth {r['min_depth_ratio']} · SG={r['same_game_legs']} missing={r['missing_quotes']} · {r['ticker']})")
