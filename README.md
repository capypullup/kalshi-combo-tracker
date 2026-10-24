# CapyEV

A phone-friendly dashboard that watches combo (parlay) bets on Kalshi and scores each one against the orderbooks of its legs. Its mascot is a capybara sipping boba (`combo_tracker/ui/capyev.svg`; `scripts/render_icons.py` turns it into the browser-tab and home-screen icons).

For every combo bet in the window it shows:

```
YES Combo +4579
────────────
NO Mookie Betts: 2+ (Milwaukee vs Los Angeles D: Home Runs)
NO Freddy Peralta: 6+ (Cleveland vs Tampa Bay: Strikeouts)
────────────
EV MID +3610.1% (fair -383 / 1.26x) · 20u
EV CRX +3568.4% (fair -363 / 1.28x) · 19u
```

## Home and the tab bar

The app opens on **Home**, a phone-first list of the best bets right now: combos of $100+ from the last hour whose fair price beats what was paid, best EV (cross) first. Each card shows EV mid / EV cross in a green badge that deepens with the edge (a pale tint under +10%, solid neon with a 🔥 from about +200%), the leagues, start time or LIVE, the bet, its legs, and big **Bet in CapyEV** and **kalshi.com** buttons. Pre-match / Live / All, Min EV and Min bet filter it.

Pages switch from a tab bar fixed to the bottom of the screen, like Kalshi's app: Combos and Bettors on the left, the cyan capybara button (Home) in the middle, the rest on the right. Tabs are listed in `NAV_TABS` in `app.py`.

## Combos page

- **Fair (mid)** is the product of the leg mid prices. **Fair (CRX)** is the product of the prices you get by crossing the spread to the other side (the leg bids). It's the conservative number. The card shows each fair price as American odds and as a payout multiplier (`fair -383 / 1.26x`).
- **EV %** = fair ÷ price paid − 1. **Units** = Kelly fraction × your Kelly multiplier ÷ unit size. The defaults are ¼ Kelly and 1u = 1% of bankroll.
- The page is a black and cyan table with rounded type and pill-shaped controls, about 10 bets per phone screen. Each row shows when the bet was placed, the side with legs / games, the bet → payout with its multiplier (American odds, contracts and contract price underneath; on a phone the payout gets its own line), the fair odds at mid and crossing the spread (Mid odds / Cross odds, as a multiplier with American odds underneath), and EV at mid and EV crossing the spread, each with Kelly units.
- Toggles at the top: **Min EV**, **Min QK** (Kelly units) and **Min bet** chips, a **Window** (15m, 1h, 3h), **Indep. only** to hide same-game combos, **Mid / Cross** to pick which EV the minimums use, **Hide thin** to drop combos with an unreliable leg book, **Pregame / Live**, and a search box for leg names. The gear icon sets the Kelly fraction, unit size and what counts as thin (default: a leg spread over 3¢ or under 50 contracts at the top of book). Your choices are remembered on your phone.
- Min bet starts at $50. Combos with a crypto, commodity or other financial leg (Bitcoin, gold, oil, stock indices, FX) are hidden everywhere in the app. Kalshi's own event category decides (Crypto, Commodities, Financials), with a list of known series (`combo_tracker/financial.py`) until a series' events have been seen.
- The list scrolls endlessly and shows the newest bets first after every refresh (the app scores the newest 600 bets in each view, after hiding financial ones). Tap a column header to sort differently, or a row to open it: each leg with its odds, bid / ask (`*` marks a thin book) and LIVE or its start time, fair odds, net EV after fees, flags, a book button that walks the full leg orderbooks at the bet's size, and **Copy bet card**.
- **Bet in CapyEV** on an open bet opens the Quote page with the bet filled in: it asks makers for quotes at your Kelly size and bets the best quote once you confirm (see Quote below).
- **kalshi.com** opens Kalshi's combo builder in your browser with that exact combo in the bet slip, via `kalshi.com/combos?marketTicker=<combo ticker>`, the link Kalshi's own shared combos use. The Kalshi app can't load a combo from a link: iOS hands `kalshi.com/markets_by_ticker/*` links to the app, which opens without the combo, and the app doesn't claim `/combos`, so this link stays in the browser (sign in to kalshi.com there once). Kalshi quotes it fresh, so compare its price with the fair odds before you buy (kalshi.com only buys YES on a combo, so a NO bet gets a reminder to use Bet in CapyEV).
- Flags: `SG` same-game legs (correlated), `THIN` a thin leg book, `IN BAND` the edge is within the leg spreads, `NO QUOTE` a leg has no market, `CHECK` an edge over +200%. LIVE uses each game's scheduled start from Kalshi's public schedule (`/milestones`); a combo is live once any leg's game has started (red dot in the row). Legs Kalshi doesn't schedule count as live within 3h of their expected settlement.

All data comes from Kalshi's free public API. No API key is needed.

## Bettors page

The **Bettors** tab is about who is behind the combo bets, starting with whales and tennis:

- Kalshi's main trade feed has no trader ID, but every combo bet starts as a **request for quote** (RFQ), which Kalshi broadcasts to logged-in users with the requester's code, a hash that stays the same for that person. When a bet fills, Kalshi closes its request in the same instant, and the close message carries the market, the size and the code. Matching each new combo trade to the request on the same market, with the same size, that closed with it names about 97% of combo bets (`combo_tracker/rfq.py`; in the live check 197 of 200 closed within 0.5 s of their trade). A quote can fill a bit less than asked, so a request for up to 2.5x the trade's size counts when nothing fits exactly. "Sure" means every request that fit and closed about as near the trade was the same person's.
- Kalshi keeps no RFQ history, so a **recorder** (`scripts/recorder_bot.py`, `.github/workflows/recorder.yml`) runs around the clock in GitHub Actions, like the notification bot: each run lasts ~5h50m and the hourly schedule queues the next one behind it. It keeps **whales** (anyone who has placed a combo bet of $5,000+, and from then on their bets of $1,000+), every **tennis**-leg combo bet of $25+ for everyone, and every bet of a **starred** bettor. Each kept bet is priced against its legs' books seconds after it fills (fair value at leg mids and crossing the spread) and settled when the combo resolves. Settled bets of ordinary bettors older than three days fold into per-day totals per code; whales' and starred bettors' bets stay whole for a year. When a bet's trade shows a public nickname in Kalshi's social feed, the code gets that name. The recorder also runs the public-profile tracker (below) and saves everything every 5 minutes to the `bettor-data` branch, encrypted with a key derived from the Kalshi private key (set `BETTOR_DATA_KEY` in both the app and GitHub to use a separate passphrase). The page reads that branch, so it loads instantly and nothing is lost when the app restarts. Thresholds are repository Variables: `WHALE_MIN`, `WHALE_TRACK_MIN`, `TENNIS_MIN`, `BETTOR_TAGS` (codes or nicknames to follow); with `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` each starred bettor's new bet is sent to Telegram.
- **Whales** and **Tennis** rank bettors by dollars bet (biggest bet under it), settled PnL after fees (ROI under it), **vs EV** (settled PnL minus what the book said those bets were worth when placed) with its **z** (around 0 is what the book expected, 2+ is hard to get by luck), and bets (share won under it), over all time, 30 days or 7 days. Whales have a size chip (their biggest bet in the period) and a **95¢+ bets** switch: locks, which risk a lot to win a little on a combo that's nearly decided, are left out until you turn them on. Tennis opens on the last 30 days, 5+ bets, sorted by z. Tap a row for the breakdown and their latest bets, with **Quote** to check an open one on the Quote page.
- **Live** lists the open bets from the last two days of whales, the top tennis bettors (by z and by dollars), anyone starred, and the public profiles the tracker follows, newest first, each with the bettor's 30-day record and a **Quote it** button.
- Combos with a crypto, commodity or other financial leg are not recorded (`combo_tracker/financial.py` decides, once it's in the tree); bets kept before their legs were known are checked and dropped on the next run.
- **★ Star** a bettor to keep every bet of theirs and to filter to them. Stars live in the phone's browser; with a GitHub token in the app's secrets (`GH_TOKEN`, a fine-grained token with contents read/write on this repo) and `TRADE_PIN` set, a session that has unlocked with the PIN writes them to the `bettor-tags` branch, which the recorder reads every minute.
- **Profiles**: Kalshi's social API (the one kalshi.com profiles use, keyless) returns trades with the maker's and taker's nicknames for anyone whose profile shows their trades. The recorder reads the last day of tennis trades from it, takes the 40 busiest public names (plus starred names), pages each one's whole history, prices every trade against the market's bid/ask mid in the minute it happened (one-minute candles) after taker fees, and settles them. **PnL** is Kalshi's own number for the profile; **Beat** is settled PnL minus EV with its z. Names you add in the search box are loaded by the app itself. Profiles that hide their trades only show Kalshi's PnL and market count. Kalshi's leaderboard isn't used because it scrambles nicknames for non-browser clients.
- Volume: at busy times Kalshi sends ~3,000 RFQs a second from ~20,000 people. The stream thread only parses the closes and keeps 30 seconds of them, and reads without websocket-client's pure-Python UTF-8 check, which otherwise caps it near Kalshi's rate (Kalshi drops a subscriber that falls behind; the recorder resubscribes). On a quiet morning there were ~35 combo bets of $5k+ an hour (most of them 95¢+ locks on tennis) and ~280 of $1k+.

### API key for RFQ matching and the recorder

Create a key on Kalshi (Account & security → API keys, with read and write access) and add two secrets, `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` (the whole private key file), in Streamlit Cloud (app Settings → Secrets, TOML: `KALSHI_KEY_ID = "..."` and `KALSHI_PRIVATE_KEY = """..."""`; the key works with or without its `-----BEGIN/END PRIVATE KEY-----` lines, RSA or Ed25519) and in GitHub (Settings → Secrets → Actions) for CI's live check. Both must come from the same Kalshi key: a key ID from one key with the private key of another gets `INCORRECT_API_KEY_SIGNATURE`. Put them above any `[section]` line (in TOML, everything after one belongs to that section; the app still finds them there, and in other capitals). The app reads secrets as they're saved, so no reboot is needed; if one is missing or misnamed, the Bettors and Quote pages say which. The Quote page's **API key check** shows the app's key ID ending and public-key fingerprint, and whether Kalshi accepts it for reading and trading.

Add a third secret, `TRADE_PIN` (a passphrase or 6+ digits), to bet from the Quote page. The app is public, so once it's set, quotes and bets ask for the PIN; a phone stays unlocked for 30 days, and more than 8 wrong PINs in an hour lock it for everyone.

## Quote page

Built for copying a bet from the Combos page: open a bet, tap **Bet in CapyEV** (or Copy ticker, then the big **Paste ticker** button). A ticker, a CapyEV `/quote?t=TICKER&side=yes&p=0.25&n=100` link or a Kalshi `combos?marketTicker=` or `markets_by_ticker` link all work.

- **Verdict**: LIVE when the combo's own orderbook has resting orders at or under the price, or the same side filled at that price in the last 90 s (the picked bet's own fills don't count); LIKELY when the newest fill was at the price a few minutes ago; MOVED when newer fills are worse; STALE when nothing has filled since.
- **Since HH:MM**: fair odds and EV when CapyEV scored the bet vs now, and which legs moved. A +EV line from two minutes ago can be gone.
- The combo's book, its newest fills, and each leg's book now, with EV at the price and at the best price available now. Watch re-checks every 5 s.
- **Quotes** (needs the API key): pasting a combo sends a real RFQ at your size, in units (1u = $1,000 and ¼ Kelly by default, editable under Sizing; the Kelly size at the best price now is preselected). Quotes list the odds after your fee, cost, EV MID/CROSS and Kelly units at that price. RFQs hide your ID from makers and close after 45 s. Kalshi allows one open request per combo, so if you have one open on the same combo (its slip in the Kalshi app, say), it's closed first.
- **Bet** (needs `TRADE_PIN`) takes the best quote after a confirm step that shows its EV: you buy at the maker's price for the quoted size, the maker confirms within 3 s, and the page shows the fill. If Kalshi stops answering mid-bet, the page says the bet may have gone through: check the Kalshi app before betting again. One bet is capped at 3u.
- **Add legs**: near-certain legs (80–98.5%, tight books) on other games, by sport, from legs people combined in the last 15 minutes. Pick some and quote the bigger combo next to the original: makers price whole combos, so an extra leg sometimes costs less than its odds.
- **Bet on kalshi.com** opens Kalshi's combo builder in your browser with the combo in the slip.

## Notification bot

`scripts/alert_bot.py` sends +EV combo bets to your phone, with separate bars for pregame and live bets (EV MID):

| Bet size | Pregame | Live |
|---|---|---|
| $50–500 | +100% | +200% |
| $500–1k | +20% | +100% |
| $1k+ | +10% | +50% |

Live bars are higher because most live edges are lines moving under a stale book. It always leaves out combos with a thin leg book (a leg spread over 3¢ or under 50 contracts at the top), and by default same-game combos (their legs aren't independent, so the fair price is off), combos of more than 5 legs (more than 3 once a game has started) or across 3+ leagues, and combos with a crypto, commodity or other financial leg (BTC, gold, oil, stock indices; see `combo_tracker/financial.py`), as a Telegram message from your own bot (free, no daily cap) or through [ntfy](https://ntfy.sh) (free, no account, but about 250 messages a day). It runs on GitHub Actions (`.github/workflows/alerts.yml`), so it keeps going while the Streamlit app sleeps.

- Each alert opens with two bold lines: what your lock screen shows, e.g. `🚀 PREGAME +812% EV · $1,250 YES 4-leg combo`, and a summary of EV, fair multiple and Kelly units at mid and crossing the spread, e.g. `MID +812% · 1.26x · 20u | CRX +640% · 1.28x · 15u`. Then bet → payout, how long ago, a same-game warning (when `ALERT_SKIP_SAME_GAME=0`) and the bet card, with a **Bet in Kalshi** button (and **Quote it** if `APP_URL` is set). The emoji grows with the edge: 🟢 under +50%, 🔥 +50%, 💰 +100%, 🚀 +500%; on ntfy the bigger ones also ring louder. 
- One alert per combo and side. It checks for new trades every minute and alerts bets with a fill in the last 5 minutes (20 minutes when a run starts); each run hands the list of what it sent to the next, so nothing repeats.
- Telegram setup: in Telegram, message **@BotFather**, send `/newbot` and pick any name; it replies with a token. Add it as the repository secret `TELEGRAM_BOT_TOKEN` (Settings → Secrets and variables → Actions → New repository secret), open your new bot and send it any message, then start the bot (Actions → Notification bot → Run workflow). Its first message tells you your chat ID; add that as `TELEGRAM_CHAT_ID` too.
- ntfy instead: install the ntfy app, subscribe to a topic name nobody would guess, and add it as the secret `NTFY_TOPIC`.
- Change the thresholds without touching code in Settings → Secrets and variables → Actions → **Variables**: `ALERT_TIERS_PREGAME` and `ALERT_TIERS_LIVE` (bet size:min EV %, defaults `50:100,500:20,1000:10` and `50:200,500:100,1000:50`; add `5000:5` for another tier, or `off` to turn one kind off), `ALERT_SKIP_SAME_GAME` (`0` to get same-game combos too), `ALERT_MAX_LEGS`, `ALERT_MAX_LEGS_LIVE` and `ALERT_MAX_LEAGUES` (defaults `5`, `3` and `2`, `off` for no cap; leagues as the app labels them, so ATP and WTA count as two), `ALERT_EV_BASIS` (`mid` or `crx`, default `mid`), `APP_URL` (your CapyEV link).
- Each run watches for about 5h50m (GitHub's limit is 6 h), and the hourly schedule queues the next run behind it. To pause it: Actions → Notification bot → ⋯ → Disable workflow. GitHub turns schedules off in a repo with no commits for 60 days; re-enable it on the same page.

## Tail bot

`scripts/tail_bot.py` follows one friend who has agreed to it and sends each of their combo bets to the same Telegram bot a few seconds after it fills (`👥 Friend bet $2,000 YES · 2-leg combo +121`, then the bet card with EV now and a **Bet in Kalshi** button). It reuses the RFQ matching above, so it needs the API key secrets as well as the Telegram ones.

- **Finding their code**: once it's running, send the bot one of their combo bets placed after it started. The easiest is the `$1,999.76 pays $4,424.51` line from a combo on their Positions tab (the payout is their contract count, which almost no one else matches to the cent); a combo link or ticker works too. If several people fit, it lists them and the next bet you send narrows it down. `/status` shows what it knows, `/forget` drops the code, `/code <code>` sets one. The code is kept between runs.
- Only combos: single-market bets go through the order book, which has no requester code. It remembers 6 hours of everyone's bets for finding a code (`TAIL_KEEP_HOURS`), handed from run to run with the code (both locked with a key from the Kalshi private key, since the Actions cache isn't private); bets placed while it was off can't be found.
- Variables (Settings → Secrets and variables → Actions → Variables): `TAIL_LABEL` (the name in alerts), `TAIL_NICKNAME` (used too if their profile shows trades publicly), `TAIL_CODES` (codes you already know), `TAIL_PEEK` (`1` also sends a silent 👀 when they ask for a quote, before they bet).
- It runs around the clock: one run lasts about 5h50m and the hourly schedule keeps the next one queued, so it takes over within a minute or two. Actions → Tail bot → Run workflow starts one by hand.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py            # live data
DEMO=1 streamlit run app.py     # made-up data, no network needed
python scripts/live_check.py 15 100   # print the top combos from the last 15 min, bets ≥ $100
python scripts/live_bettors.py 12 400 # one small pass of the profile tracker
DRY_RUN=1 RECORDER_MINUTES=3 python scripts/recorder_bot.py  # needs the API key; keeps and prices bets, saves nothing
DRY_RUN=1 ALERT_MINUTES=5 python scripts/alert_bot.py  # print alerts for 5 min instead of sending
DRY_RUN=1 TAIL_SELFTEST=1 TAIL_MINUTES=3 python scripts/tail_bot.py  # needs the API key; prints instead of sending
pytest -q
```

## Put it on your phone (free)

1. Push this repo to GitHub.
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**, pick this repo, and set the main file to `app.py`.
3. Open the URL it gives you on your phone and use **Add to Home Screen**.

A background worker pulls new combo trades every minute and re-scores the filters people have open every 2 minutes, so the page never waits on Kalshi and doesn't change while you're reading it. Tap ↻ to refresh now: the worker pulls the newest trades and leg quotes and re-scores your filter first, and the icon spins until the new bets are in (10–15 seconds on a busy night; the table stays usable meanwhile). The header says how old the scores are. The worker also keeps every Min bet at your window scored, so switching between $5 and $500 is instant; a new window takes a few seconds the first time. Nobody looking means no Kalshi calls: after 5 minutes without an open page the worker pauses. When you come back after a while, the page brews fresh scores (it loads the last 15 minutes of trades first, which takes 10–40 seconds depending on how busy Kalshi is) rather than showing old ones; older history fills in over the next few minutes. Streamlit Community Cloud puts apps to sleep after a while with no visitors. Opening the link wakes it up in about 30 seconds.

Kalshi's public API needs no key but limits reads per IP: the trade feed and the event list take about 5 calls a second each, the market lookups 25 or more. `combo_tracker/client.py` paces every client in the app under one shared limit and those two routes under their own, so the app doesn't get rate limited (HTTP 429).

## Layout

- `combo_tracker/client.py`: Kalshi API calls (trades, markets, events, orderbooks)
- `combo_tracker/orderbook.py`: per-leg book math (bid/ask/mid, depth, walking the book)
- `combo_tracker/engine.py`: groups fills into bets and computes fair prices, EV, Kelly and flags
- `combo_tracker/format.py`: the text bet card
- `combo_tracker/links.py`: the link that puts a market in a Kalshi bet slip (kalshi.com's combo builder for a combo)
- `combo_tracker/pipeline.py`: one refresh, end to end
- `combo_tracker/view.py`: the compact rows the table receives
- `combo_tracker/home.py`: the Home page's picks and cards
- `combo_tracker/bettors.py`: the Bettors tracker (tennis feed discovery, histories, EV vs the book, PnL)
- `combo_tracker/rfq.py`: the RFQ stream and RFQ-to-trade matching
- `combo_tracker/recorder.py`: the recorder's store (whales, tennis bettors, starred bettors), its tables, the encrypted data branch and tags
- `combo_tracker/alerts.py`: which bets the notification bot sends and how the alerts read
- `combo_tracker/financial.py`: which legs are crypto, commodities or other financial markets (hidden in the app and alerts)
- `combo_tracker/tail.py`: the tail bot's memory of matched bets, finding a friend's code, and its messages
- `combo_tracker/quote.py` + `quote_view.py` + `ui/paste.js`: the Quote page (live check, RFQs, betting, extra legs)
- `combo_tracker/ui/`: the table components (filters, sorting and copying run in the browser)
- `app.py`: the Streamlit page
