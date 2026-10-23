"""Bettor recorder: names the people behind combo bets and keeps the ones that matter for the Bettors page.

Listens to Kalshi's RFQ stream (needs KALSHI_KEY_ID + KALSHI_PRIVATE_KEY), names the requester of
every combo bet (combo_tracker/rfq.py) and keeps (combo_tracker/recorder.py): whales' bets (a bet of
WHALE_MIN, $5,000, makes a whale; their later bets of WHALE_TRACK_MIN, $1,000, count too), every
tennis-leg combo bet of TENNIS_MIN ($25) or more, and every bet of a tagged bettor. Each kept bet is
priced against its legs' books right away and settled when its combo resolves. It also runs the
public-profile tracker (combo_tracker/bettors.py: the busiest public names in Kalshi's tennis feed,
their whole histories, PnL vs EV) and links codes to public names. Every few minutes everything is
saved, encrypted, to the bettor-data branch.

Tags come from the bettor-tags branch (the app writes tags.json there when it has a GitHub token)
and from the BETTOR_TAGS variable (codes or nicknames, comma-separated). With TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_ID, each new bet of a tagged bettor is sent to Telegram.

.github/workflows/recorder.yml keeps it running around the clock. The log only shows totals, since
the repo (and so the log) is public.

Settings: WHALE_MIN, WHALE_TRACK_MIN, TENNIS_MIN, RECORDER_MINUTES (one run, default 350),
RECORDER_SAVE_EVERY (seconds between saves, default 300), DRY_RUN=1 (save nowhere, send nothing;
CI uses it), BETTOR_DATA_KEY (optional: lock the data with this instead of the Kalshi key; the app
needs it too), APP_URL (adds a Quote it button to alerts).

python scripts/recorder_bot.py
"""

import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker import recorder  # noqa: E402
from combo_tracker.alerts import TELEGRAM_URL  # noqa: E402
from combo_tracker.bettors import MONEY_UNIT, BettorTracker  # noqa: E402
from combo_tracker.client import KalshiAuth, KalshiClient  # noqa: E402
from combo_tracker.pipeline import Scorer  # noqa: E402
from combo_tracker.rfq import RfqBook  # noqa: E402
from combo_tracker.tail import Tail, trades_of  # noqa: E402
from combo_tracker.view import pack_bettors  # noqa: E402


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


whale_min = float(setting("WHALE_MIN", str(recorder.WHALE_MIN)))
track_min = float(setting("WHALE_TRACK_MIN", str(recorder.TRACK_MIN)))
tennis_min = float(setting("TENNIS_MIN", str(recorder.TENNIS_MIN)))
minutes = float(setting("RECORDER_MINUTES", "350"))
save_every = float(setting("RECORDER_SAVE_EVERY", "300"))
dry = setting("DRY_RUN", "0") == "1"
secret = recorder.secret_key()
app_url = setting("APP_URL") or None
token, chat_id = setting("TELEGRAM_BOT_TOKEN"), setting("TELEGRAM_CHAT_ID")
tg = f"{TELEGRAM_URL}/bot{token}" if token and chat_id and not dry else None

auth = KalshiAuth.from_env()
if auth is None:
    sys.exit("The recorder needs a Kalshi API key: set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY.")

print(f"Recorder: whales bet ${whale_min:,.0f}+ (then ${track_min:,.0f}+ counts), tennis bets ${tennis_min:,.0f}+; "
      f"{minutes:.0f} min; data locked with {recorder.key_label(auth, secret)}; "
      f"{'dry run' if dry else 'saving to ' + recorder.BRANCH}; alerts {'on' if tg else 'off'}", flush=True)

store = recorder.Store(whale_min, track_min, tennis_min)
saved = recorder.fetch_files(names=(recorder.STORE_FILE,)).get(recorder.STORE_FILE)
if saved:
    try:
        store = recorder.Store.from_dict(recorder.unseal(saved, auth, secret), whale_min, track_min, tennis_min)
        print(f"Picked up {len(store.bets):,} bets, {len(store.codes):,} codes, "
              f"{sum(map(store.is_whale, store.codes)):,} whales ({len(saved) / 1e6:.1f} MB)", flush=True)
    except recorder.Locked as exc:
        if not dry:
            sys.exit(f"Can't open the saved data: {exc}. Put the same key back, or delete the "
                     f"{recorder.BRANCH} branch to start over.")
        print(f"Saved data: {exc}", flush=True)
else:
    print("No saved data yet; starting fresh.", flush=True)

client = KalshiClient(rate_per_sec=6, auth=auth)
scorer = Scorer(client, quote_ttl=15)
book = RfqBook(client)
tracker = BettorTracker(KalshiClient(rate_per_sec=4), every=900)  # public profiles, on its own thread
pending: deque = deque()  # (bet, match) waiting to be priced
counts = dict.fromkeys(("kept", "priced", "unpriced", "settled", "folded", "linked", "lookups", "alerts", "errors"), 0)
sent: dict[str, float] = {}  # alert keys -> when


# ---- tags ----
def read_tags() -> None:
    tags = recorder.parse_tags(recorder.fetch_files(branch=recorder.TAGS_BRANCH, names=(recorder.TAGS_FILE,))
                               .get(recorder.TAGS_FILE))
    tags |= {t.strip().lstrip("@#") for t in setting("BETTOR_TAGS").split(",") if t.strip()}
    new = tags - store.tags
    store.tags = tags
    for tag in new:
        if not recorder.is_code(tag):
            tracker.add(tag)  # a nickname: the profile tracker loads their history
    if new:
        print(f"{time.strftime('%H:%M:%S')} tags: {len(store.tags)} ({len(new)} new)", flush=True)


# ---- alerts ----
def send(payload: dict) -> None:
    if tg is None:
        print(">>>", payload.get("text"), flush=True)
        return
    for _ in range(2):
        resp = requests.post(f"{tg}/sendMessage", json=payload, timeout=15)
        if resp.status_code != 429:
            break
        time.sleep(float(resp.json().get("parameters", {}).get("retry_after", 3)))
    if not resp.ok:
        print("telegram:", resp.status_code, resp.text[:200], flush=True)


def alert(bet: dict, m, rows: list[dict]) -> None:
    """A tagged bettor placed a bet: say what, how big, EV now."""
    key = f"{bet['tk']}|{bet['s']}|{int(bet['t'] // 60)}"
    if key in sent or not (tg or dry):
        return
    sent[key] = time.time()
    label = store.codes.get(bet["c"], {}).get("name") or f"#{bet['c'][:8]}"
    tail = Tail(label)
    payload = tail.bet_message(rows[0], chat_id, app_url, time.time() - bet["t"]) if rows else tail.plain_bet_message(m, chat_id)
    send(payload)
    counts["alerts"] += 1


# ---- jobs ----
def price_some(limit: int = 6) -> None:
    for _ in range(min(limit, len(pending))):
        bet, m = pending.popleft()
        try:
            rows = scorer.score(trades_of(m), max_bets=1).rows
        except Exception as exc:
            counts["errors"] += 1
            print(f"{time.strftime('%H:%M:%S')} pricing error: {exc!r}"[:200], flush=True)
            rows = []
        if rows:
            store.price(bet, rows[0])
            counts["priced"] += 1
        else:
            counts["unpriced"] += 1
        if store.is_tagged(bet["c"]):
            alert(bet, m, rows)


def settle() -> None:
    tickers = store.unsettled()[:4000]
    if tickers:
        counts["settled"] += store.settle(client.get_markets(tickers, batch_size=100))
    counts["folded"] += store.fold()


def link(limit: int = 12) -> None:
    # Names we already know from public histories: a kept bet whose trade shows a taker there.
    fresh = time.time() - 2 * recorder.DAY  # a kept bet is at most a couple of days old when it's still unnamed
    takers = {f.trade_id: p.nickname for p in list(tracker.profiles.values()) if not p.code
              for f in list(p.fills.values()) if f.taker and f.ts >= fresh}
    for b in store.bets.values():
        if b["id"] in takers and not store.codes.get(b["c"], {}).get("name"):
            store.name(b["c"], takers[b["id"]])
            counts["linked"] += 1
    for bet in store.to_link()[:limit]:
        counts["lookups"] += 1
        data = client.social_trades(market_ticker=bet["tk"])
        counts["linked"] += bool(store.link(bet, data.get("trades") or []))


def profiles(limit: int = 20, every: float = 6 * 3600) -> None:
    """Kalshi's own PnL numbers for bettors with a public name."""
    now = time.time()
    named = [i for i in store.codes.values() if i.get("name") and now - i.get("pat", 0) > every][:limit]
    for info in named:
        info["pat"] = now
        try:
            for key, days in (("pa", None), ("p30", 30)):
                v = client.profile_metrics(info["name"], days=days).get("pnl")
                info[key] = None if v is None else round(v / MONEY_UNIT, 2)
        except requests.HTTPError:
            info["pa"] = info["p30"] = None


def profile_live(now: float, hours: float = recorder.LIVE_HOURS) -> list[dict]:
    """Open positions from the last two days of the public profiles the tracker follows."""
    out = []
    for p in list(tracker.profiles.values()):
        if p.code or not p.sources or p.hidden or p.missing:
            continue
        for f in list(p.fills.values()):
            m = tracker.markets.get(f.ticker) or {}
            if now - f.ts > hours * 3600 or m.get("final"):
                continue
            fair_yes = tracker.fair.get(f.trade_id)
            fair = None if fair_yes is None else (fair_yes if f.side == "yes" else 1 - fair_yes)
            ev = None if fair is None else round((fair - f.price) * f.count - f.fee, 2)
            out.append({"id": f.trade_id, "t": int(f.ts * 1000), "tk": f.ticker, "s": f.side, "n": f.count, "p": f.price,
                        "fm": fair, "L": len(m.get("legs") or []) or None, "ti": m.get("title"), "r": None, "ev": ev,
                        "name": p.nickname, "tk_taker": f.taker})
    out.sort(key=lambda b: -b["t"])
    return out[:recorder.MAX_LIVE]


def save(now: float) -> None:
    store.note_run(started, now)
    store.prune(now)
    view = recorder.build_view(store, now)
    snap = tracker.snapshot
    profiles_doc = {"v": 1, "saved": int(now * 1000), "status": tracker.status,
                    "rows": pack_bettors(snap.rows) if snap else [], "live": profile_live(now),
                    "feed_trades": snap.feed_trades if snap else 0, "named_trades": snap.named_trades if snap else 0,
                    "feed_since": int(snap.feed_since.timestamp() * 1000) if snap and snap.feed_since else None}
    files = {recorder.STORE_FILE: recorder.seal({**store.to_dict(), "saved": int(now * 1000)}, auth, secret),
             recorder.VIEW_FILE: recorder.seal(view, auth, secret),
             recorder.PROFILES_FILE: recorder.seal(profiles_doc, auth, secret)}
    sizes = ", ".join(f"{k} {len(v) / 1e6:.2f} MB" for k, v in files.items())
    if dry:
        opened = recorder.unseal(files[recorder.VIEW_FILE], auth, secret)
        print(f"  dry run: not saved ({sizes}); the view opens again with {len(opened['rows'])} bettors, "
              f"{len(opened['live'])} live bets, {len(profiles_doc['rows'])} profiles", flush=True)
        return
    recorder.push_files(files, f"Bettor data {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}")
    print(f"  saved {sizes}: {view['bettors']} bettors ({view['whales']} whales), {len(view['live'])} live bets, "
          f"{len(profiles_doc['rows'])} profiles", flush=True)


def run_tracker() -> None:
    while not stopping.is_set():
        try:
            tracker.cycle()
        except Exception as exc:
            print(f"{time.strftime('%H:%M:%S')} profiles error: {exc!r}"[:200], flush=True)
        for _ in range(int(tracker.every)):
            if stopping.is_set():
                return
            tracker.serve_wanted()
            time.sleep(1)


scorer.starts.refresh()  # the game schedule, so a bet's first pricing isn't slow
read_tags()
stopping = threading.Event()
threading.Thread(target=run_tracker, daemon=True, name="profile-tracker").start()
book.start()
started = time.time()
deadline = started + minutes * 60
last = {"save": started, "settle": started - 540, "link": started, "profiles": started, "tags": started, "log": started}
jobs = [("settle", 600, settle), ("link", 120, link), ("profiles", 1800, profiles), ("tags", 60, read_tags)]
while time.time() < deadline:
    try:
        for m in book.take():
            bet = store.consider(m)
            if bet is not None:
                pending.append((bet, m))
                counts["kept"] += 1
        price_some()
        now = time.time()
        for name, every, job in jobs:
            if now - last[name] >= every:
                last[name] = now
                try:
                    job()
                except Exception as exc:  # keep recording through API hiccups
                    counts["errors"] += 1
                    print(f"{time.strftime('%H:%M:%S')} {name} error: {exc!r}"[:200], flush=True)
        if now - last["log"] >= (60 if dry else 300):
            last["log"] = now
            s = book.stats
            print(f"{time.strftime('%H:%M:%S')} stream={book.ws_status} rfqs={s['rfqs']:,} combo bets={s['executions']:,} "
                  f"named={s['sure'] + s['likely']:,} | this run: kept {counts['kept']}, priced {counts['priced']} "
                  f"({counts['unpriced']} not), settled {counts['settled']}, folded {counts['folded']}, named "
                  f"{counts['linked']} of {counts['lookups']} looked up, alerts {counts['alerts']}, errors {counts['errors']} | "
                  f"whales {sum(map(store.is_whale, store.codes)):,}, codes {len(store.codes):,}, bets {len(store.bets):,}, "
                  f"waiting {len(pending)} | profiles: {tracker.status}, {len(tracker.profiles)} | stream error: {book.last_error}",
                  flush=True)
        if now - last["save"] >= save_every:
            last["save"] = now
            save(now)
    except Exception as exc:
        counts["errors"] += 1
        print(f"{time.strftime('%H:%M:%S')} error: {exc!r}", flush=True)
        traceback.print_exc(limit=3, file=sys.stdout)
    time.sleep(0.5)

stopping.set()
book.stop()
price_some(limit=len(pending))
save(time.time())
