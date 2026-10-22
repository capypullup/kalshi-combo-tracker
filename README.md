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
- Flags: `SG` same-game legs (correlated), `THIN` shallow leg books, `IN BAND` the edge is within the leg spreads, `NO QUOTE` a leg has no market, `CHECK` an edge over +200%.
- The page is a compact table, about 10 bets per phone screen. Tap a column header to sort, or tap a row to open its bet card with the full leg orderbooks. Toggles at the top: min bet size chips, quick filters (+EV, Clean, No SG, No thin, YES only) and ⚙️ More filters (window, custom min, max legs, Kelly settings).

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

The app pulls fresh data every 3 minutes while it's open, and you can tap ↻ to refresh now. Streamlit Community Cloud puts apps to sleep after a while with no visitors. Opening the link wakes it up in about 30 seconds.

## Layout

- `combo_tracker/client.py`: Kalshi API calls (trades, markets, events, orderbooks)
- `combo_tracker/orderbook.py`: per-leg book math (bid/ask/mid, depth, walking the book)
- `combo_tracker/engine.py`: groups fills into bets and computes fair prices, EV, Kelly and flags
- `combo_tracker/format.py`: the text bet card
- `combo_tracker/pipeline.py`: one refresh, end to end
- `app.py`: the Streamlit UI
