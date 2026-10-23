"""Listen to live RFQs for a few minutes, match them to new combo trades, and report how well it worked.

Needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY in the environment; skips otherwise.
Usage: python scripts/live_rfq.py [seconds]
"""

import json
import statistics
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))

from combo_tracker.bettors import BettorTracker  # noqa: E402
from combo_tracker.client import KalshiAuth, KalshiClient  # noqa: E402
from combo_tracker.engine import taker_fee  # noqa: E402
from combo_tracker.rfq import PLACEHOLDER, RfqBook  # noqa: E402


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def settled_sample(client) -> None:
    """What Kalshi reports for combos bet on about a day and a half ago: status, result, settlement fields."""
    from datetime import datetime, timedelta, timezone
    from itertools import islice

    try:
        now = datetime.now(timezone.utc)
        trades = list(islice(client.iter_trades(now - timedelta(hours=36), now - timedelta(hours=35, minutes=55)), 20000))
        combos = [t for t in trades if t["ticker"].startswith("KXMVE")]
        tickers = list(dict.fromkeys(t["ticker"] for t in combos))[:300]
        markets = client.get_markets(tickers, batch_size=100)
        print(f"\n== settled combos? == {len(trades)} trades 36h ago, {len(combos)} combo fills, {len(markets)} markets looked up")
        print("status x result:", dict(Counter((m.get("status"), m.get("result")) for m in markets.values())))
        keys = sorted({k for m in markets.values() for k in m if "settle" in k or "result" in k or "expir" in k})
        print("settlement-ish keys:", keys)
        odd = [m for m in markets.values() if m.get("result") not in ("yes", "no")][:3]
        for m in odd or list(markets.values())[:1]:
            print("  sample:", {k: m.get(k) for k in ["ticker", "status", "result", *keys]})
    except Exception as exc:
        print("settled sample failed:", repr(exc))


def big_bets(taken, missed, elapsed) -> None:
    """How many big combo bets there are, how well they match, and who places them."""
    stake = lambda e: e.count * e.price  # noqa: E731
    print("\n== big bets ==")
    per_hour = 3600 / max(elapsed, 1)
    for cut in (1000, 2500, 5000, 10000, 25000):
        named = [m for m in taken if stake(m.execution) >= cut]
        lost = [e for e, _ in missed if stake(e) >= cut]
        print(f">=${cut:,}: {len(named)} named, {len(lost)} unmatched ({(len(named) + len(lost)) * per_hour:,.0f}/h), "
              f"sure {sum(m.sure for m in named)}, partial {sum(m.partial for m in named)}, "
              f"codes {len({m.creator for m in named})}")
    whales = [m for m in taken if stake(m.execution) >= 5000]
    prices = Counter("<20c" if m.execution.price < 0.2 else "<50c" if m.execution.price < 0.5 else
                     "<80c" if m.execution.price < 0.8 else "<95c" if m.execution.price < 0.95 else "95c+"
                     for m in whales)
    print("$5k+ by price:", dict(prices), "| sides:", dict(Counter(m.execution.side for m in whales)),
          "| tennis:", sum(m.tennis for m in whales))
    codes = {m.creator for m in whales}
    mine = [m for m in taken if m.creator in codes]
    print(f"codes with a $5k+ bet: {len(codes)}; all their bets: {len(mine)} "
          f"(${sum(stake(m.execution) for m in mine):,.0f}); of those >=$1k: "
          f"{sum(stake(m.execution) >= 1000 for m in mine)}")
    for m in sorted(whales, key=lambda m: -stake(m.execution))[:15]:
        e = m.execution
        print(f"  ${stake(e):>9,.0f} {e.side:>3} @{e.price:.3f} x{e.count:,.0f} fills={len(e.trade_ids)} sure={m.sure} "
              f"partial={m.partial} lag={m.lag:+.2f} {m.creator[:6]} {e.ticker[:40]}")
    for e, near in [x for x in missed if stake(x[0]) >= 2500][:10]:
        print(f"  MISSED ${stake(e):,.0f} {e.side} @{e.price:.3f} x{e.count:,.0f} fills={len(e.trade_ids)} "
              f"nearest={'-' if near is None else (round(near[0] - e.ts, 2), near[2], near[3])} {e.ticker[:40]}")


def main() -> None:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    auth = KalshiAuth.from_env()
    if auth is None:
        print("No KALSHI_KEY_ID / KALSHI_PRIVATE_KEY set; skipping the RFQ check.")
        return
    client = KalshiClient(rate_per_sec=4, auth=auth)
    try:
        page = client.get_rfqs()
        rows = page.get("rfqs") or []
        print("REST /communications/rfqs:", len(rows), "rows; fields:", sorted({k for r in rows for k in r}))
    except Exception as exc:
        print("REST failed:", repr(exc))

    settled_sample(client)

    book = RfqBook(client)
    book.missed = deque(maxlen=20_000)  # keep every miss, to see how big bets match
    kinds = Counter()
    samples = {}
    orig = book.on_message

    def counting(raw):
        kind = raw[9:raw.index('"', 9)] if raw.startswith('{"type":"') else "?"
        kinds[kind] += 1
        if kind not in samples:
            samples[kind] = raw[:700]
        return orig(raw)

    book.on_message = counting
    print(f"RSS before the stream: {rss_mb():.0f} MB")
    book.start()
    taken = []
    t0, cpu0, last = time.time(), time.process_time(), Counter()
    while time.time() - t0 < seconds:
        time.sleep(30)
        taken += book.take()
        s = book.stats
        rate = {k: f"{(v - last[k]) / 30:,.0f}/s" for k, v in kinds.items()}
        last = Counter(kinds)
        done = s["sure"] + s["likely"] + s["unmatched"]
        print(f"{time.time() - t0:5.0f}s stream={book.ws_status} msgs {rate} held={book.held():,} "
              f"bets={s['executions']:,} named={s['sure'] + s['likely']:,}/{done:,} sure={s['sure']:,} "
              f"hidden={s['hidden']} revealed={s['revealed']} overflows={s['overflows']} "
              f"rss={rss_mb():.0f}MB cpu={100 * (time.process_time() - cpu0) / (time.time() - t0):.0f}% "
              f"err={book.last_error}")
    time.sleep(book.settle + book.wait + book.poll_seconds)  # let the last trades finish matching
    book.stop()
    taken += book.take()

    print("\n== stream samples ==")
    for kind, raw in samples.items():
        print(kind, raw)
    elapsed = time.time() - t0
    s = dict(book.stats)
    named = s["sure"] + s["likely"]
    print("\n== summary ==")
    print(json.dumps(s))
    print(f"combo bets (executions) seen: {s['executions']:,}; named {named:,} "
          f"({named / max(1, named + s['unmatched']):.1%}), sure {s['sure'] / max(1, named):.1%} of those; "
          f"RFQs heard: {s['rfqs']:,} ({s['rfqs'] / elapsed:,.0f}/s)")

    recent = list(book.recent)
    if recent:
        lags = sorted(m.lag for m in recent)
        print("close - trade (s) over the last", len(lags), "matches: min", f"{lags[0]:.2f}",
              "median", f"{statistics.median(lags):.2f}", "max", f"{lags[-1]:.2f}",
              "| within 0.5s:", sum(abs(x) <= 0.5 for x in lags))
    stakes = [m.execution.count * m.execution.price for m in taken]
    if stakes:
        cuts = [5, 10, 25, 50, 100, 250, 1000]
        print("bet sizes:", {f"<${c}": sum(x < c for x in stakes) for c in cuts}, f">=${cuts[-1]}:",
              sum(x >= cuts[-1] for x in stakes))
    print("tennis bets named:", sum(m.tennis for m in taken))

    missed = list(book.missed)
    why = Counter()
    for e, near in missed:
        if near is None:
            why["no RFQ closed on that combo"] += 1
        elif abs(near[0] - e.ts) > 3:
            why["nearest close >3s away"] += 1
        else:
            why["close in time, size differs"] += 1
    print("unmatched (last", len(missed), "):", dict(why))
    for e, near in missed[:12]:
        cost = e.count * e.price
        print(f"  {e.ticker[:44]} {e.side} {e.count}@{e.price:.3f} cost ${cost:.2f} (+fee ${cost + taker_fee(e.price, e.count):.2f})"
              f" fills={len(e.trade_ids)} nearest={'-' if near is None else (round(near[0] - e.ts, 2), near[2], near[3], near[1][:8])}")

    by = defaultdict(lambda: [0, 0.0])
    for m in taken:
        by[m.creator][0] += 1
        by[m.creator][1] += m.execution.count * m.execution.price
    print("codes:", len(by), "| top by bets:", sorted(((v[0], round(v[1]), k[:10]) for k, v in by.items()), reverse=True)[:8])
    print("top by $:", sorted(((round(v[1]), v[0], k[:10]) for k, v in by.items()), reverse=True)[:8])
    print("hidden left unrevealed:", sum(m.creator == PLACEHOLDER for m in taken))

    big_bets(taken, list(book.missed), elapsed)

    pending = list(taken)
    tracker = BettorTracker(client, rfq=SimpleNamespace(take=lambda: [pending.pop() for _ in range(len(pending))]),
                            links_per_cycle=40)
    tracker.sync_codes()
    tracker.link_codes()
    linked = [(p.nickname, p.linked) for p in tracker.code_profiles() if p.linked]
    print("linked codes:", linked[:10], "of", len(tracker.top_codes()), "tried")
    print(f"RSS at the end: {rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
