# Kalshi Combo EV Tracker

A phone-friendly dashboard that watches combo (parlay) bets on Kalshi and scores each one against the orderbooks of its legs.

For every combo bet in the window it shows:

```
YES Combo +4579
────────────
NO Mookie Betts: 2+ (Milwaukee vs Los Angeles D: Home Runs)
NO Freddy Peralta: 6+ (Cleveland vs Tampa Bay: Strikeouts)
────────────
EV MID +3610.1% (fair -383) · 20u
EV CRX +3568.4% (fair -363) · 19u
```

- **Fair (mid)** is the product of the leg mid prices. **Fair (CRX)** is the product of the prices you get by crossing the spread to the other side (the leg bids). It's the conservative number.
- **EV %** = fair ÷ price paid − 1. **Units** = Kelly fraction × your Kelly multiplier ÷ unit size. The defaults are ¼ Kelly and 1u = 1% of bankroll.
- The page is a black and cyan table, about 10 bets per phone screen. Each row shows when the bet was placed, the side with legs / games, the bet → payout with its multiplier (American odds, contracts and contract price underneath), and EV at mid and EV crossing the spread, each with Kelly units.
- Toggles at the top: **MIN EV**, **MIN QK** (Kelly units) and **MIN BET** chips, a **WINDOW** (15m, 1h, 3h), **INDEP. ONLY** to hide same-game combos, **MID / CROSS** to pick which EV the minimums use, **HIDE THIN** to drop combos with an unreliable leg book, **PREGAME / LIVE**, and a search box for leg names. The gear icon sets the Kelly fraction, unit size and what counts as thin (default: a leg spread over 3¢ or under 50 contracts at the top of book). Your choices are remembered on your phone.
- The list scrolls endlessly and shows the newest bets first after every refresh (the app scores the newest 600 bets in each view). Tap a column header to sort differently, or a row to open it: each leg with its odds, bid / ask (`*` marks a thin book) and LIVE or its start time, fair odds, net EV after fees, flags, a book button that walks the full leg orderbooks at the bet's size, and **Copy bet card**.
- Flags: `SG` same-game legs (correlated), `THIN` a thin leg book, `IN BAND` the edge is within the leg spreads, `NO QUOTE` a leg has no market, `CHECK` an edge over +200%. LIVE uses each game's scheduled start from Kalshi's public schedule (`/milestones`); a combo is live once any leg's game has started (red dot in the row). Legs Kalshi doesn't schedule count as live within 3h of their expected settlement.

All data comes from Kalshi's free public API. No API key is needed.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py            # live data
DEMO=1 streamlit run app.py     # made-up data, no network needed
python scripts/live_check.py 15 100   # print the top combos from the last 15 min, bets ≥ $100
pytest -q
```

## Put it on your phone (free)

1. Push this repo to GitHub.
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**, pick this repo, and set the main file to `app.py`.
3. Open the URL it gives you on your phone and use **Add to Home Screen**.

A background worker pulls new combo trades every minute and re-scores the filters people have open every 2 minutes, so the page never waits on Kalshi and doesn't change while you're reading it. Tap ↻ to show the latest scores; the header says how old they are. Streamlit Community Cloud puts apps to sleep after a while with no visitors. Opening the link wakes it up in about 30 seconds.

## Layout

- `combo_tracker/client.py`: Kalshi API calls (trades, markets, events, orderbooks)
- `combo_tracker/orderbook.py`: per-leg book math (bid/ask/mid, depth, walking the book)
- `combo_tracker/engine.py`: groups fills into bets and computes fair prices, EV, Kelly and flags
- `combo_tracker/format.py`: the text bet card
- `combo_tracker/pipeline.py`: one refresh, end to end
- `combo_tracker/view.py`: the compact rows the table receives
- `combo_tracker/ui/`: the table component (filters, sorting and copying run in the browser)
- `app.py`: the Streamlit page
