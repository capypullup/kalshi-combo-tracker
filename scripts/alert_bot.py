"""Notification bot: push every combo bet over $X with EV above Y% to your phone.

Runs a loop for ALERT_MINUTES (the GitHub workflow restarts it, see .github/workflows/alerts.yml):
pull the newest combo trades every minute, score the bets against the leg books, and send each
new one that clears its size tier to your
Telegram bot (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) or ntfy topic (NTFY_TOPIC). Pregame and live
bets have their own tiers (ALERT_TIERS_PREGAME, ALERT_TIERS_LIVE: "50:100,500:20" = $50+ bets need
+100% EV, $500+ need +20%; live defaults to $1k+ at +100%, $5k+ at +50%; "off" turns one kind off). Thin leg
books are left out, and so are same-game combos (ALERT_SKIP_SAME_GAME=0 keeps them), combos over 5 legs
(ALERT_MAX_LEGS), over 3 legs once a game has started (ALERT_MAX_LEGS_LIVE) or across 3+ leagues
(ALERT_MAX_LEAGUES=2), "off" for no cap, and combos with a crypto leg. The app's Alerts tool saves
settings to alerts.SETTINGS_FILE on the alert-settings branch; they win over the variables and are
re-read every minute (GITHUB_TOKEN reads them fresh). With neither sender set (or DRY_RUN=1) it prints
the alerts instead. ALERT_SENT_FILE keeps what was sent, so the next run doesn't repeat it.

python scripts/alert_bot.py
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker import alerts, financial  # noqa: E402
from combo_tracker.client import KalshiClient  # noqa: E402
from combo_tracker.collector import Collector  # noqa: E402
from combo_tracker.ghfile import GitHubFile  # noqa: E402
from combo_tracker.pipeline import Scorer  # noqa: E402


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


base = alerts.Settings.from_env(os.environ)  # the ALERT_* variables over the defaults
store = GitHubFile(alerts.SETTINGS_FILE, alerts.SETTINGS_BRANCH, token=setting("GITHUB_TOKEN") or None)
minutes = float(setting("ALERT_MINUTES", "350"))
every = float(setting("ALERT_EVERY", "60"))
app_url = setting("APP_URL") or None
sent_file = Path(setting("ALERT_SENT_FILE", "alerts_sent.json"))
window = 20  # minutes of bets looked at on start-up; covers the hand-off between two runs
fresh = 5  # after that, only bets with a fill in the last 5 min (older ones whose EV drifts up are stale news)

sender = None
if setting("DRY_RUN", "0") != "1":
    if setting("TELEGRAM_BOT_TOKEN"):
        sender = alerts.Telegram(setting("TELEGRAM_BOT_TOKEN"), setting("TELEGRAM_CHAT_ID") or None)
    elif setting("NTFY_TOPIC"):
        sender = alerts.Ntfy(setting("NTFY_TOPIC"))
print(f"{base.describe()} · every {every:.0f}s for {minutes:.0f} min · "
      f"{type(sender).__name__ if sender else 'dry run (printing only)'}", flush=True)

app_settings = alerts.AppSettings(store, base, log=lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True))

# ticker|side -> when it was sent; kept 12 hours
sent: dict[str, float] = {}
try:
    sent = {k: t for k, t in json.loads(sent_file.read_text()).items() if time.time() - t < 12 * 3600}
    print(f"{len(sent)} alerts already sent in the last 12h", flush=True)
except (OSError, ValueError):
    pass

client = KalshiClient()
col = Collector(client, max_window_minutes=window, initial_backfill_minutes=window)
scorer = Scorer(client, quote_ttl=every / 2, hide=financial.is_financial)  # no lookups for crypto combos
deadline = time.time() + minutes * 60
ok = 0
while time.time() < deadline:
    start = time.time()
    try:
        s = app_settings.get()
        min_bet = s.min_bet()
        col.poll()
        if min_bet is None:  # pregame and live both off
            ok += 1
            print(f"{time.strftime('%H:%M:%S')} pregame and live alerts are both off", flush=True)
            time.sleep(max(0.0, min(every - (time.time() - start), deadline - time.time())))
            continue
        trades = col.window(window)
        if ok:  # after start-up, only score combos with a fill in the last few minutes
            recent = {t["ticker"] for t in col.window(fresh)}
            trades = [t for t in trades if t["ticker"] in recent]
        snap = scorer.score(trades, min_stake=min_bet, max_bets=5000)
        rows = snap.rows if not ok else [r for r in snap.rows if r["minutes_ago"] <= fresh]
        hits = alerts.pick(rows, s, set(sent))
        for row in hits:
            thin_at = (s.thin_spread, s.thin_size)  # so the "thin leg book" note matches the filter
            if sender:
                sender.send(row, s.basis, app_url, thin_at)
            else:
                print(f"\n=== {alerts.title(row, s.basis)}\n{alerts.body(row, thin_at)}", flush=True)
            sent[alerts.key(row)] = time.time()
        sent_file.write_text(json.dumps(sent))
        ok += 1
        print(f"{time.strftime('%H:%M:%S')} {snap.bets_over_min} bets ≥ ${min_bet:,.0f}, {snap.bets_scored} scored, "
              f"{len(hits)} alerted ({time.time() - start:.0f}s, {client.requests_made} requests)", flush=True)
    except Exception as exc:  # keep watching through API hiccups
        print(f"{time.strftime('%H:%M:%S')} error: {exc!r}", flush=True)
        traceback.print_exc(limit=3, file=sys.stdout)
    time.sleep(max(0.0, min(every - (time.time() - start), deadline - time.time())))

sys.exit(0 if ok else 1)
