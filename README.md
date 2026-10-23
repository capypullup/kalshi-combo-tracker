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

The app opens on **Home**, a phone-first list of the best bets right now: combos of $100+ from the last hour whose fair price beats what was paid, best EV (cross) first. Each card shows EV mid / EV cross in a green badge that deepens with the edge (a pale tint under +10%, solid neon with a 🔥 from about +200%), the leagues, start time or LIVE, the bet, its legs, and big **Bet in Kalshi** and **Quote** buttons. Pre-match / Live / All, Min EV and Min bet filter it.

Pages switch from a tab bar fixed to the bottom of the screen, like Kalshi's app: Combos and Bettors on the left, the cyan capybara button (Home) in the middle, the rest on the right. Tabs are listed in `NAV_TABS` in `app.py`.

## Combos page

- **Fair (mid)** is the product of the leg mid prices. **Fair (CRX)** is the product of the prices you get by crossing the spread to the other side (the leg bids). It's the conservative number. The card shows each fair price as American odds and as a payout multiplier (`fair -383 / 1.26x`).
- **EV %** = fair ÷ price paid − 1. **Units** = Kelly fraction × your Kelly multiplier ÷ unit size. The defaults are ¼ Kelly and 1u = 1% of bankroll.
- The page is a black and cyan table with rounded type and pill-shaped controls, about 10 bets per phone screen. Each row shows when the bet was placed, the side with legs / games, the bet → payout with its multiplier (American odds, contracts and contract price underneath; on a phone the payout gets its own line), the fair odds at mid and crossing the spread (Mid odds / Cross odds, as a multiplier with American odds underneath), and EV at mid and EV crossing the spread, each with Kelly units.
- Toggles at the top: **Min EV**, **Min QK** (Kelly units) and **Min bet** chips, a **Window** (15m, 1h, 3h), **Indep. only** to hide same-game combos, **Mid / Cross** to pick which EV the minimums use, **Hide thin** to drop combos with an unreliable leg book, **Pregame / Live**, and a search box for leg names. The gear icon sets the Kelly fraction, unit size and what counts as thin (default: a leg spread over 3¢ or under 50 contracts at the top of book). Your choices are remembered on your phone.
- The list scrolls endlessly and shows the newest bets first after every refresh (the app scores the newest 600 bets in each view). Tap a column header to sort differently, or a row to open it: each leg with its odds, bid / ask (`*` marks a thin book) and LIVE or its start time, fair odds, net EV after fees, flags, a book button that walks the full leg orderbooks at the bet's size, and **Copy bet card**.
- **Bet in Kalshi** on an open bet opens that exact combo in the Kalshi app, ready to buy. It links to `kalshi.com/markets_by_ticker/<combo ticker>`, a path Kalshi's iOS and Android apps open; without the app, kalshi.com opens its combo builder with the combo in the bet slip. Kalshi quotes it fresh, so compare its price with the fair odds before you buy (a NO bet gets a reminder to check the side). **Quote it** opens the Quote page with the bet filled in.
- Flags: `SG` same-game legs (correlated), `THIN` a thin leg book, `IN BAND` the edge is within the leg spreads, `NO QUOTE` a leg has no market, `CHECK` an edge over +200%. LIVE uses each game's scheduled start from Kalshi's public schedule (`/milestones`); a combo is live once any leg's game has started (red dot in the row). Legs Kalshi doesn't schedule count as live within 3h of their expected settlement.

All data comes from Kalshi's free public API. No API key is needed.

## Bettors page

The **Bettors** tab ranks public Kalshi profiles, with tennis bettors as the starting point:

- Kalshi's main trade feed has no trader ID, but its social API (the one kalshi.com profiles use, also keyless) returns the same trades with the maker's and taker's nicknames for anyone whose profile shows their trades. The tracker reads the last day of tennis trades from it, takes the 40 busiest public names, and pages each one's whole history. You can add any nickname from the search box.
- **PnL** is Kalshi's own number for the profile. **Trades** is the size of our sample.
- **EV** prices every trade against the market's bid/ask mid in the minute it happened (from one-minute candles), after taker fees; a combo is priced from its legs. **Beat** is settled PnL minus EV on the same trades, and **z** is that divided by its standard deviation, so a high z over many trades means the bettor beats the book by more than luck explains.
- **Scope** switches between tennis-only and all of a bettor's trades. Tap a row for the breakdown and their latest trades.
- **View** switches between all-time and the last 30 days (Kalshi's PnL for that period, and our sample over it); **Trades** shows win % underneath. Each bettor's whole history is loaded (long ones fill in over a few 15-minute cycles; a `+` after the count means more is coming). EV is priced for the last 30 days of trades.
- **RFQ codes** (needs an API key, see below): every combo bet starts as a request for quote, which Kalshi broadcasts to logged-in users with the requester's code, a hash that stays the same for that person. When a bet fills, Kalshi closes its request in the same instant, and the close message carries the market, the size and the code. The app listens to that stream, polls the trade feed every 5 seconds, and matches each new combo trade to the request on the same market, with the same size, that closed within a couple of seconds of it (in the live check, 197 of 200 closed within 0.5 s of their trade). A quote can fill a bit less than asked, so a request for up to 2.5x the trade's size counts when nothing fits exactly. "Sure" means every request that fit and closed about as near the trade was the same person's (the Kalshi app re-asks every few seconds while a slip is open). That gives every combo bettor, public or not, a stable ID with their bets, EV, PnL and win rate. When one of a code's trades shows a public nickname in the social feed, the code is linked to that name. Codes only build up while the app is running, because Kalshi doesn't keep RFQ history.
- Volume: at busy times Kalshi sends ~3,000 RFQs a second from ~20,000 people. The stream thread only parses the closes and keeps 30 seconds of them (~40 MB at peak), and reads without websocket-client's pure-Python UTF-8 check, which otherwise caps it near Kalshi's rate (Kalshi drops a subscriber that falls behind; the app resubscribes if that happens). Every code keeps its bets, but only the 100 biggest by dollars bet, plus the 50 biggest in tennis, with 3+ bets are priced and listed; past 30,000 codes the smallest idle ones are dropped.
- Limits: profiles that hide their trades only show Kalshi's PnL and market count. Kalshi's leaderboard isn't used because it scrambles nicknames for non-browser clients.

### API key for RFQ matching

Create a key on Kalshi (Account & security → API keys) and add two secrets, `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` (the whole private key file), in Streamlit Cloud (app Settings → Secrets, TOML: `KALSHI_KEY_ID = "..."` and `KALSHI_PRIVATE_KEY = """..."""`; the key works with or without its `-----BEGIN/END PRIVATE KEY-----` lines, RSA or Ed25519) and in GitHub (Settings → Secrets → Actions) for CI's live check. The app only makes read requests with it, except the Quote page's Get quote button, which creates and deletes your own RFQs.

## Quote page

A fill in the feed means someone got that price, not that you still can. The QUOTE tab checks one combo now (pick a recent bet or paste a combo ticker; `?t=TICKER&side=yes&p=0.25&n=100` on `/quote` pre-fills it):

- **Verdict**: LIVE when the combo's own orderbook has resting orders at or under the price, or the same side filled at that price in the last 90 s (the picked bet's own fills don't count); LIKELY when the newest fill was at the price a few minutes ago; MOVED when newer fills are worse; STALE when nothing has filled since.
- The combo's book, its newest fills, and each leg's book now, with EV at the price and at the best price available now. Watch re-checks every 5 s.
- **Get quote** (needs the API key above) sends a real RFQ for $10–$500, lists makers' quotes as they arrive with price incl. fee, odds, size, maker code and EV MID/CROSS, and cancels the RFQ after 45 s. It never accepts a quote. This is the one place the app sends signed write requests (create/delete your own RFQ).

## Notification bot

`scripts/alert_bot.py` sends big +EV combo bets to your phone: **$500–1k bets above +20% EV MID** and **$1k+ bets above +10%**, leaving out combos with a thin leg book (a leg spread over 3¢ or under 50 contracts at the top) and combos with a crypto leg (BTC, ETH, SOL, XRP, DOGE and other crypto price markets; the list is `SKIP_SERIES` in `combo_tracker/alerts.py`), as a Telegram message from your own bot (free, no daily cap) or through [ntfy](https://ntfy.sh) (free, no account, but about 250 messages a day). It runs on GitHub Actions (`.github/workflows/alerts.yml`), so it keeps going while the Streamlit app sleeps.

- Each alert leads with what your lock screen shows, e.g. `🚀 +812% EV · $1,250 YES 4-leg combo`, then bet → payout, how long ago, LIVE or pregame, a same-game warning and the bet card, with a **Bet in Kalshi** button (and **Quote it** if `APP_URL` is set). The emoji grows with the edge: 🟢 under +50%, 🔥 +50%, 💰 +100%, 🚀 +500%; on ntfy the bigger ones also ring louder. 
- One alert per combo and side. It checks for new trades every minute and alerts bets with a fill in the last 5 minutes (20 minutes when a run starts); each run hands the list of what it sent to the next, so nothing repeats.
- Telegram setup: in Telegram, message **@BotFather**, send `/newbot` and pick any name; it replies with a token. Add it as the repository secret `TELEGRAM_BOT_TOKEN` (Settings → Secrets and variables → Actions → New repository secret), open your new bot and send it any message, then start the bot (Actions → Notification bot → Run workflow). Its first message tells you your chat ID; add that as `TELEGRAM_CHAT_ID` too.
- ntfy instead: install the ntfy app, subscribe to a topic name nobody would guess, and add it as the secret `NTFY_TOPIC`.
- Change the thresholds without touching code in Settings → Secrets and variables → Actions → **Variables**: `ALERT_TIERS` (bet size:min EV %, default `500:20,1000:10`; add `5000:5` for another tier), `ALERT_SKIP_THIN` (`0` to get thin books too), `ALERT_EV_BASIS` (`mid` or `crx`, default `mid`), `APP_URL` (your CapyEV link).
- Each run watches for about 5h50m (GitHub's limit is 6 h), and the hourly schedule queues the next run behind it. To pause it: Actions → Notification bot → ⋯ → Disable workflow. GitHub turns schedules off in a repo with no commits for 60 days; re-enable it on the same page.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py            # live data
DEMO=1 streamlit run app.py     # made-up data, no network needed
python scripts/live_check.py 15 100   # print the top combos from the last 15 min, bets ≥ $100
python scripts/live_bettors.py 12 400 # one small pass of the Bettors tracker
DRY_RUN=1 ALERT_MINUTES=5 python scripts/alert_bot.py  # print alerts for 5 min instead of sending
pytest -q
```

## Put it on your phone (free)

1. Push this repo to GitHub.
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**, pick this repo, and set the main file to `app.py`.
3. Open the URL it gives you on your phone and use **Add to Home Screen**.

A background worker pulls new combo trades every minute and re-scores the filters people have open every 2 minutes, so the page never waits on Kalshi and doesn't change while you're reading it. Tap ↻ to refresh now: the worker pulls the newest trades and leg quotes and re-scores your filter first, and the icon spins until the new bets are in (10–15 seconds on a busy night; the table stays usable meanwhile). The header says how old the scores are. Streamlit Community Cloud puts apps to sleep after a while with no visitors. Opening the link wakes it up in about 30 seconds.

## Layout

- `combo_tracker/client.py`: Kalshi API calls (trades, markets, events, orderbooks)
- `combo_tracker/orderbook.py`: per-leg book math (bid/ask/mid, depth, walking the book)
- `combo_tracker/engine.py`: groups fills into bets and computes fair prices, EV, Kelly and flags
- `combo_tracker/format.py`: the text bet card
- `combo_tracker/links.py`: the link that opens a market in Kalshi's app
- `combo_tracker/pipeline.py`: one refresh, end to end
- `combo_tracker/view.py`: the compact rows the table receives
- `combo_tracker/home.py`: the Home page's picks and cards
- `combo_tracker/bettors.py`: the Bettors tracker (tennis feed discovery, histories, EV vs the book, PnL)
- `combo_tracker/rfq.py`: the RFQ stream and RFQ-to-trade matching
- `combo_tracker/alerts.py`: which bets the notification bot sends and how the alerts read
- `combo_tracker/quote.py` + `quote_view.py`: the Quote page (live check, RFQs)
- `combo_tracker/ui/`: the table components (filters, sorting and copying run in the browser)
- `app.py`: the Streamlit page
