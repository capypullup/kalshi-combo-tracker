"""Notification bot: push every combo bet over $X with EV above Y% to your phone.

Runs a loop for ALERT_MINUTES (the GitHub workflow restarts it, see .github/workflows/alerts.yml):
pull the newest combo trades every minute, score bets of at least ALERT_MIN_BET dollars
against the leg books, and send each new one with EV above ALERT_MIN_EV percent to the
ntfy topic NTFY_TOPIC. With no topic (or DRY_RUN=1) it prints the alerts instead.

python scripts/alert_bot.py
"""

import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker import alerts  # noqa: E402
from combo_tracker.client import KalshiClient  # noqa: E402
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.pipeline import Scorer  # noqa: E402


def setting(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


topic = setting("NTFY_TOPIC", "")
dry = setting("DRY_RUN", "0") == "1" or not topic
min_bet = float(setting("ALERT_MIN_BET", "500"))
min_ev = float(setting("ALERT_MIN_EV", "10")) / 100
basis = setting("ALERT_EV_BASIS", "mid").lower()  # "mid" or "crx" (crossing the spread)
minutes = float(setting("ALERT_MINUTES", "350"))
every = float(setting("ALERT_EVERY", "60"))
app_url = setting("APP_URL", "") or None
window = 20  # minutes of bets looked at; covers the hand-off between two runs

print(f"bets ≥ ${min_bet:,.0f} with EV {basis.upper()} > {min_ev * 100:.0f}% · every {every:.0f}s for {minutes:.0f} min · "
      f"{'dry run (printing only)' if dry else 'sending to ntfy'}", flush=True)

client = KalshiClient()
col = Collector(client, max_window_minutes=window, initial_backfill_minutes=window)
scorer = Scorer(client, quote_ttl=every / 2)
sent: set[str] = set()
if not dry:
    try:
        sent = alerts.sent_keys(topic)
        print(f"{len(sent)} alerts already sent in the last 12h", flush=True)
    except Exception as exc:  # worst case a restart repeats a few alerts
        print(f"couldn't read the topic's history: {exc!r}", flush=True)

deadline = time.time() + minutes * 60
ok = 0
while time.time() < deadline:
    start = time.time()
    try:
        col.poll()
        snap = scorer.score(col.window(window), min_stake=min_bet, max_bets=1000)
        hits = alerts.pick(snap.rows, min_bet, min_ev, sent, basis)
        for row in hits:
            msg = alerts.message(row, topic, basis, app_url)
            if dry:
                print(f"\n=== {msg['title']} (priority {msg['priority']})\n{msg['message']}\n{msg['click']}", flush=True)
            else:
                alerts.send(msg)
            sent.add(alerts.key(row))
        ok += 1
        print(f"{time.strftime('%H:%M:%S')} {snap.bets_over_min} bets ≥ ${min_bet:,.0f}, {snap.bets_scored} scored, "
              f"{len(hits)} alerted ({time.time() - start:.0f}s, {client.requests_made} requests)", flush=True)
    except Exception as exc:  # keep watching through API hiccups
        print(f"{time.strftime('%H:%M:%S')} error: {exc!r}", flush=True)
        traceback.print_exc(limit=3, file=sys.stdout)
    time.sleep(max(0.0, min(every - (time.time() - start), deadline - time.time())))

sys.exit(0 if ok else 1)
