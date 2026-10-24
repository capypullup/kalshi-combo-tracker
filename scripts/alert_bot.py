"""Notification bot: push every combo bet over $X with EV above Y% to your phone.

Runs a loop for ALERT_MINUTES (the GitHub workflow restarts it, see .github/workflows/alerts.yml):
pull the newest combo trades every minute, score the bets against the leg books, and send each
new one that clears its size tier to your
Telegram bot (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) or ntfy topic (NTFY_TOPIC). Pregame and live
bets have their own tiers (ALERT_TIERS_PREGAME, ALERT_TIERS_LIVE: "50:100,500:20" = $50+ bets need
+100% EV, $500+ need +20%; "off" turns one kind off). Thin leg books are always left out, and so are same-game combos
(ALERT_SKIP_SAME_GAME=0 keeps them), combos over 5 legs (ALERT_MAX_LEGS), over 3 legs once a game
has started (ALERT_MAX_LEGS_LIVE) or across 3+ leagues (ALERT_MAX_LEAGUES=2), "off" for no cap, and
combos with a crypto leg. With
neither set (or DRY_RUN=1) it prints the alerts instead. ALERT_SENT_FILE keeps what was
sent, so the next run doesn't repeat it.

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
from combo_tracker.pipeline import Scorer  # noqa: E402


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def cap(name: str, default: int) -> int | None:
    """A whole-number limit; "off" means none."""
    text = setting(name, str(default))
    return None if text.lower() == "off" else int(text)


pregame = alerts.parse_tiers(setting("ALERT_TIERS_PREGAME", alerts.PREGAME_TIERS))
live = alerts.parse_tiers(setting("ALERT_TIERS_LIVE", alerts.LIVE_TIERS))
skip_same_game = setting("ALERT_SKIP_SAME_GAME", "1") != "0"
max_legs = cap("ALERT_MAX_LEGS", alerts.MAX_LEGS)
max_legs_live = cap("ALERT_MAX_LEGS_LIVE", alerts.MAX_LEGS_LIVE)
max_leagues = cap("ALERT_MAX_LEAGUES", alerts.MAX_LEAGUES)
min_bet = min(stake for stake, _ in pregame + live)
basis = setting("ALERT_EV_BASIS", "mid").lower()  # "mid" or "crx" (crossing the spread)
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
def rules(tiers):
    return ", ".join(f"${stake:,.0f}+ at {ev * 100:+.0f}%" for stake, ev in tiers) or "off"


skips = [name for name, on in (("no thin books", True), ("no same-game legs", skip_same_game),
                               (f"up to {max_legs} legs", max_legs is not None),
                               (f"up to {max_legs_live} legs live", max_legs_live is not None),
                               (f"up to {max_leagues} leagues", max_leagues is not None)) if on]
print(f"EV {basis.upper()} pregame: {rules(pregame)} · live: {rules(live)} · {' · '.join(skips + [''])}every {every:.0f}s for {minutes:.0f} min · "
      f"{type(sender).__name__ if sender else 'dry run (printing only)'}", flush=True)

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
        col.poll()
        trades = col.window(window)
        if ok:  # after start-up, only score combos with a fill in the last few minutes
            recent = {t["ticker"] for t in col.window(fresh)}
            trades = [t for t in trades if t["ticker"] in recent]
        snap = scorer.score(trades, min_stake=min_bet, max_bets=5000)
        rows = snap.rows if not ok else [r for r in snap.rows if r["minutes_ago"] <= fresh]
        hits = alerts.pick(rows, pregame, live, set(sent), basis, skip_same_game=skip_same_game,
                           max_legs=max_legs, max_leagues=max_leagues, max_legs_live=max_legs_live)
        for row in hits:
            if sender:
                sender.send(row, basis, app_url)
            else:
                print(f"\n=== {alerts.title(row, basis)}\n{alerts.body(row)}", flush=True)
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
