"""Listen to live RFQs for a few minutes, match them to combo trades, and print what matched.

Needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY in the environment; skips otherwise.
Usage: python scripts/live_rfq.py [seconds]
"""

import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from combo_tracker.bettors import BettorTracker  # noqa: E402
from combo_tracker.client import KalshiAuth, KalshiClient  # noqa: E402
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.rfq import PLACEHOLDER, RfqBook  # noqa: E402


def main() -> None:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    auth = KalshiAuth.from_env()
    if auth is None:
        print("No KALSHI_KEY_ID / KALSHI_PRIVATE_KEY set; skipping the RFQ check.")
        return
    client = KalshiClient(rate_per_sec=8, auth=auth)

    print("== REST: first page of /communications/rfqs ==")
    try:
        page = client.get_rfqs()
        rows = page.get("rfqs") or []
        print("keys:", sorted(page), "count:", len(rows))
        for r in rows[:3]:
            print(json.dumps(r)[:600])
        print("created_ts order:", [r.get("created_ts") for r in rows[:5]], "...", [r.get("created_ts") for r in rows[-2:]])
        print("statuses:", Counter(r.get("status") for r in rows))
    except Exception as exc:
        print("REST failed:", repr(exc))

    collector = Collector(client, initial_backfill_minutes=5)
    collector.poll()
    book = RfqBook(client, collector.window)
    raw_counts = Counter()
    orig = book.on_message

    def counting(raw):
        msg = json.loads(raw)
        raw_counts[msg.get("type")] += 1
        if raw_counts[msg.get("type")] <= 2:
            print("stream sample:", raw[:500])
        orig(raw)

    book.on_message = counting
    book.start()
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(30)
        collector.poll()
        book.match_now()
        print(f"{time.time() - t0:5.0f}s stream={book.ws_status} msgs={dict(raw_counts)} rfqs={len(book.rfqs)} "
              f"combo trades={len(collector.trades)} matches={dict(book.stats)} err={book.last_error}")
    book.stop()
    time.sleep(20)  # let the last RFQs' trades print
    collector.poll()
    book.match_now()
    try:
        book.poll_rest(max_pages=5)
        book.match_now()
    except Exception as exc:
        print("REST poll failed:", repr(exc))

    rfqs = list(book.rfqs.values())
    print("\n== summary ==")
    print("rfqs:", len(rfqs), "closed:", sum(r.closed is not None for r in rfqs),
          "hidden creator:", sum(r.creator == PLACEHOLDER for r in rfqs),
          "distinct creators:", len({r.creator for r in rfqs}),
          "sized by contracts / cost:", sum(bool(r.contracts) for r in rfqs), "/", sum(bool(r.target) for r in rfqs))
    print("stats:", dict(book.stats))
    by = book.by_creator()
    print("codes with bets:", len(by), "top:", sorted(((len(v), k[:10]) for k, v in by.items()), reverse=True)[:10])
    for m in list({id(m): m for m in book.matches.values()}.values())[:10]:
        r = book.rfqs.get(m.rfq_id)
        e = m.execution
        print(f"  {'SURE ' if m.sure else 'likely'} {m.creator[:10]} {e.ticker[:40]} {e.side} {e.count}@{e.price:.3f} "
              f"trade+{e.ts - r.created:.1f}s after RFQ, close {'-' if r.closed is None else f'{r.closed - e.ts:+.1f}s'}"
              f" size={r.contracts or r.target}")
    combo_execs = len({t[1] for t in collector.trades.values()})
    print("combo trades in window:", combo_execs, "matched trades:", len(book.matches))

    tracker = BettorTracker(client, rfq=book, links_per_cycle=30)
    tracker.sync_codes()
    tracker.link_codes()
    codes = tracker.code_profiles()
    print("linked codes:", [(p.nickname, p.linked) for p in codes if p.linked][:10], "of", len(codes))


if __name__ == "__main__":
    main()
