"""Tail bot: send a friend's combo bets (with their OK) to your Telegram a few seconds after they fill.

Listens to Kalshi's RFQ stream (needs KALSHI_KEY_ID + KALSHI_PRIVATE_KEY), names the
requester of each combo bet (combo_tracker/rfq.py) and sends the ones from the friend's
code (combo_tracker/tail.py) to the same Telegram bot as the notification bot
(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

Finding the friend's code: send the bot one of their combo bets placed while it was running,
as "$1,999.76 pays $4,424.51" from their Positions tab, or as a combo link or ticker. A
second one narrows it down if several people fit. /status, /forget and /code <code> also
work. TAIL_CODES takes codes you already know (comma-separated), and TAIL_NICKNAME (their nickname) is used
if their profile shows their trades publicly.

Other settings: TAIL_LABEL (name in the alerts, default the nickname or "Friend"),
TAIL_PEEK=1 (also ping quietly when they ask for a quote, before they bet),
TAIL_MINUTES (how long one run lasts), TAIL_KEEP_HOURS (how far back a bet you send can be,
default 6), TAIL_STATE_FILE and TAIL_HISTORY_FILE (the codes, and the last hours of everyone's
bets, handed to the next run; both locked with a key derived from the Kalshi private key), APP_URL (adds a Quote it button),
DRY_RUN=1 (print instead of sending), TAIL_SELFTEST=1 (CI: once bets come in, find one by its
payout as if you'd sent it and print its alert, so the whole path runs).

The repo is public, and so is the Actions log: outside a dry run the log only shows counts and
timings, never the friend's code, nickname or bets.

python scripts/tail_bot.py
"""

import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from combo_tracker import recorder  # noqa: E402
from combo_tracker.alerts import TELEGRAM_URL  # noqa: E402
from combo_tracker.client import KalshiAuth, KalshiClient  # noqa: E402
from combo_tracker.pipeline import Scorer  # noqa: E402
from combo_tracker.rfq import RfqBook  # noqa: E402
from combo_tracker.tail import Tail, payouts_in, short, tickers_in, trades_of  # noqa: E402


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


nickname = setting("TAIL_NICKNAME")
tail = Tail(setting("TAIL_LABEL", nickname or "Friend"), float(setting("TAIL_KEEP_HOURS", "6")))
peek = setting("TAIL_PEEK", "0") == "1"
minutes = float(setting("TAIL_MINUTES", "350"))
app_url = setting("APP_URL") or None
state_file = Path(setting("TAIL_STATE_FILE", "tail_state.sealed"))
history_file = Path(setting("TAIL_HISTORY_FILE", "tail_history.sealed"))
dry = setting("DRY_RUN", "0") == "1"
selftest = setting("TAIL_SELFTEST", "0") == "1"
token, chat_id = setting("TELEGRAM_BOT_TOKEN"), setting("TELEGRAM_CHAT_ID")
tg = f"{TELEGRAM_URL}/bot{token}" if token and chat_id and not dry else None

auth = KalshiAuth.from_env()
if auth is None:
    sys.exit("The tail bot needs a Kalshi API key: set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY.")

offset = 0
try:
    saved = recorder.unseal(state_file.read_bytes(), auth)
    tail.load(saved)
    offset = saved.get("telegram_offset") or 0
except (OSError, ValueError, KeyError, TypeError, recorder.Locked):  # none yet, or unreadable: start fresh
    pass
try:
    print(f"{tail.load_history(recorder.unseal(history_file.read_bytes(), auth)):,} bets from the last run", flush=True)
except (OSError, ValueError, KeyError, TypeError, recorder.Locked):  # none yet, or unreadable: start fresh
    pass
for code in filter(None, (c.strip() for c in setting("TAIL_CODES").split(","))):
    tail.adopt(code, "TAIL_CODES")


def save() -> None:
    # The Actions cache can be read by other workflows in the repo, so the code is locked like the recorder's data.
    state_file.write_bytes(recorder.seal({**tail.state(), "telegram_offset": offset}, auth))


def save_history() -> None:
    history_file.write_bytes(recorder.seal(tail.dump_history(), auth))


def send(payload: dict) -> None:
    if tg is None:
        # A dry run prints the message; a real run without Telegram secrets mustn't put it in the public log.
        print("\n>>>", payload.get("text") if dry else f"(not sent: no Telegram secrets, {len(payload.get('text') or '')} chars)",
              flush=True)
        return
    for _ in range(2):
        resp = requests.post(f"{tg}/sendMessage", json=payload, timeout=15)
        if resp.status_code != 429:
            break
        time.sleep(float(resp.json().get("parameters", {}).get("retry_after", 3)))
    if not resp.ok:
        print("telegram:", resp.status_code, resp.text[:200], flush=True)


def say(text: str, quiet: bool = False) -> None:
    send({"chat_id": chat_id, "text": text, "disable_notification": quiet})


client = KalshiClient(rate_per_sec=8, auth=auth)
scorer = Scorer(client, quote_ttl=10)
book = RfqBook(client, poll_seconds=2.0, settle=1.5)

# The friend's quote requests, spotted in the stream thread before they're bets.
asked: deque = deque(maxlen=200)
heard = book.on_message


def on_message(raw: str):
    if peek and tail.codes and raw.startswith('{"type":"rfq_created"') and any(c in raw for c in list(tail.codes)):
        body = json.loads(raw).get("msg") or {}
        if body.get("creator_id") in tail.codes and body.get("market_ticker"):
            asked.append(body["market_ticker"])
    return heard(raw)


book.on_message = on_message


# ---- social feed: are the friend's trades public? ----
def social_page() -> dict:
    return client.social_trades(nickname=nickname)


public = False  # their profile shows their trades to everyone
if nickname:
    try:
        page = social_page()
        public = page.get("visibility_state") != "hidden" and page.get("trades") is not None
        print(f"social feed for TAIL_NICKNAME: {'public' if public else 'not public'}", flush=True)
    except Exception as exc:  # the URL names them: say only what kind
        print(f"social feed for TAIL_NICKNAME: {type(exc).__name__}", flush=True)
tail.link_trades = public  # trade ids are only needed to link their public trades; skipping them saves memory
seen_social: set[str] = set()


def check_social() -> None:
    page = social_page()
    trades = page.get("trades") or []
    fresh = [t for t in trades if t["trade_id"] not in seen_social]
    seen_social.update(t["trade_id"] for t in trades)
    for code in tail.learn_social(fresh, nickname):
        say(f"Found {tail.label}'s code from their trades: {short(code)}. Following it now.")


# ---- Telegram commands ----
HOW_TO = (f"To find {tail.label}'s code, send me one of their combo bets placed while I'm running: the "
          "\"$1,999.76 pays $4,424.51\" line from their Positions tab, or the combo's link or ticker.")


def resolve(text: str) -> list[str]:
    """Tickers in a message; a Kalshi share link without one is followed to where it lands."""
    found = tickers_in(text)
    if not found:
        for url in (w for w in text.split() if "kalshi" in w and w.startswith("http")):
            try:
                found += tickers_in(requests.get(url, timeout=10, allow_redirects=True).url)
            except requests.RequestException:
                pass
    return found


def handle(text: str) -> None:
    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd in ("/status", "/start", "/help"):
        say(f"{tail.status(book.stats)}\n\n{HOW_TO} /forget clears the code, /code <code> sets it.")
    elif cmd == "/forget":
        tail.codes.clear()
        tail.candidates = None
        say("Forgot their code. Send me one of their combo bets to find it again.")
    elif cmd == "/code" and len(text.split()) > 1:
        full = [c for c in {b[1] for bets in tail.history.values() for b in bets} if c.startswith(text.split()[1])]
        code = full[0] if len(full) == 1 else text.split()[1]
        tail.adopt(code, "/code")
        say(f"Following code {short(code)}.")
    else:
        payouts = payouts_in(text)
        tickers = [] if payouts else resolve(text)
        if not payouts and not tickers:
            say(f"{HOW_TO} /status shows what I know.")
        for cost, payout in payouts:
            say(tail.learn_payout(cost, payout))
        for t in tickers:
            say(tail.learn_ticker(t))
    save()


def check_telegram() -> None:
    global offset
    if tg is None:
        return
    resp = requests.get(f"{tg}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=15)
    for u in resp.json().get("result", []) if resp.ok else []:
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat_id or msg.get("date", 0) < time.time() - 1800:
            continue  # only you can steer it, and a message older than 30 min is stale
        if msg.get("text"):
            handle(msg["text"])
        elif msg.get("photo") or msg.get("document"):
            say("I can't read screenshots. Type the \"$X pays $Y\" line from the combo instead.")


# ---- alerts ----
def alert(m) -> None:
    e = m.execution
    k = tail.alert_key(e.ticker, e.side, e.ts)
    if k in tail.sent:
        return
    tail.sent[k] = time.time()
    try:
        rows = scorer.score(trades_of(m), max_bets=1).rows
    except Exception as exc:
        print("score:", type(exc).__name__, flush=True)
        rows = []
    payload = (tail.bet_message(rows[0], chat_id, app_url, time.time() - e.ts) if rows
               else tail.plain_bet_message(m, chat_id))
    send(payload)
    print(f"{time.strftime('%H:%M:%S')} alert sent {time.time() - e.ts:.1f}s after the bet", flush=True)


print(f"Tail bot: {len(tail.codes)} code(s) known; "
      f"peek={'on' if peek else 'off'}; {'Telegram' if tg else 'printing only'}; {minutes:.0f} min", flush=True)
if not tail.codes and not selftest:
    say(f"Tail bot is on but doesn't know {tail.label}'s code yet. {HOW_TO}", quiet=True)

scorer.starts.refresh()  # load the game schedule now so the first alert isn't slow
book.start()
deadline = time.time() + minutes * 60
last = {"telegram": 0.0, "social": 0.0, "prune": time.time(), "history": time.time()}
while time.time() < deadline:
    try:
        for m in book.take():
            tail.add(m)
            if selftest and time.time() - book.started > 60:
                # Find this bet by its payout as if you'd sent it, print its alert once, then let go of the code.
                selftest = False
                e = m.execution
                said = tail.learn_payout(round(e.count * e.price, 2), round(e.count, 2))
                print(f"selftest: ${e.count * e.price:,.2f} pays ${e.count:,.2f} -> {said}", flush=True)
                if tail.is_friend(m):
                    alert(m)
                tail.codes.clear()
                continue
            if tail.is_friend(m):
                alert(m)
        while asked:
            ticker = asked.popleft()
            if ticker not in tail.peeked:
                tail.peeked[ticker] = time.time()
                send(tail.peek_message(ticker, chat_id))
        now = time.time()
        if now - last["telegram"] >= 3:
            last["telegram"] = now
            check_telegram()
        if public and now - last["social"] >= 10:
            last["social"] = now
            check_social()
        if now - last["prune"] >= 60:
            last["prune"] = now
            tail.prune()
            save()
            if now - last["history"] >= 600:
                last["history"] = now
                save_history()
            s = book.stats
            print(f"{time.strftime('%H:%M:%S')} stream={book.ws_status} rfqs={s['rfqs']:,} named={s['sure'] + s['likely']:,} "
                  f"held={tail.held():,} codes={len(tail.codes)} err={book.last_error}", flush=True)
    except Exception as exc:  # keep tailing through API hiccups; a message could hold a ticker or name
        print(f"{time.strftime('%H:%M:%S')} error: {type(exc).__name__}", flush=True)
    time.sleep(0.5)

book.stop()
save()
save_history()
