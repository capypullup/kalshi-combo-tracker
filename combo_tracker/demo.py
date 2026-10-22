"""Offline stand-in for KalshiClient with made-up but realistic-shaped data.

Used for tests and for previewing the UI without network access (DEMO=1).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

GAMES = [
    ("KXNFLGAME", "26OCT26KCBUF", ["KC", "BUF"], "{} wins", "Kansas City vs Buffalo: Winner"),
    ("KXNBAGAME", "26OCT21LALBOS", ["LAL", "BOS"], "{} wins", "Los Angeles L vs Boston: Winner"),
    ("KXNBAGAME", "26OCT21DENUTA", ["DEN", "UTA"], "{} wins", "Denver vs Utah: Winner"),
    ("KXMLBGAME", "26OCT21NYYLAD", ["NYY", "LAD"], "{} wins", "New York Y vs Los Angeles D: Winner"),
    ("KXNHLGAME", "26OCT21STLCHI", ["STL", "CHI"], "{} wins", "St. Louis vs Chicago: Winner"),
    ("KXNFLTOTAL", "26OCT26KCBUF", ["47", "50"], "Over {} points", "Kansas City vs Buffalo: Total Points"),
    ("KXNBATOTAL", "26OCT21LALBOS", ["221", "228"], "Over {} points", "Los Angeles L vs Boston: Total Points"),
    ("KXMLBHR", "26OCT21MILLAD", ["Mookie Betts: 2", "Shohei Ohtani: 1"], "{}+", "Milwaukee vs Los Angeles D: Home Runs"),
    ("KXMLBKS", "26OCT21CLETB", ["Freddy Peralta: 6", "Tanner Bibee: 5"], "{}+", "Cleveland vs Tampa Bay: Strikeouts"),
]


class DemoClient:
    def __init__(self, seed: int = 7, n_trades: int = 400):
        rng = random.Random(seed)
        now = datetime.now(timezone.utc)
        self.leg_markets: dict[str, dict] = {}
        self.books: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        for series, game, outcomes, label, event_title in GAMES:
            self.events[f"{series}-{game}"] = {"event_ticker": f"{series}-{game}", "title": event_title}
            for o in outcomes:
                ticker = f"{series}-{game}-{o.split(':')[0].replace(' ', '').upper()[:8]}"
                p = rng.uniform(0.25, 0.75)
                spread = rng.choice([0.01, 0.01, 0.02, 0.03, 0.06])
                thin = rng.random() < 0.25
                yes = [[f"{max(0.01, p - spread / 2 - 0.01 * i):.4f}", f"{rng.uniform(5, 40 if thin else 2000):.2f}"] for i in range(4)]
                no = [[f"{max(0.01, 1 - p - spread / 2 - 0.01 * i):.4f}", f"{rng.uniform(5, 40 if thin else 2000):.2f}"] for i in range(4)]
                self.books[ticker] = {"yes_dollars": yes[::-1], "no_dollars": no[::-1]}
                self.leg_markets[ticker] = {"ticker": ticker, "event_ticker": f"{series}-{game}",
                                            "yes_sub_title": label.format(o), "status": "active",
                                            "yes_bid_dollars": yes[0][0], "yes_bid_size_fp": yes[0][1],
                                            "yes_ask_dollars": f"{1 - float(no[0][0]):.4f}",
                                            "yes_ask_size_fp": no[0][1]}
        tickers = list(self.leg_markets)
        self.combo_markets: dict[str, dict] = {}
        self.trades: list[dict] = []
        for i in range(n_trades // 3):
            legs = rng.sample(tickers, rng.choice([2, 2, 3, 3, 4, 5, 6]))
            cticker = f"KXMVECROSSCATEGORY-S2026DEMO{i:04d}-{i:04d}"
            sides = {t: rng.choice(["yes", "yes", "no"]) for t in legs}
            self.combo_markets[cticker] = {
                "ticker": cticker,
                "yes_sub_title": ",".join(f"{sides[t]} {self.leg_markets[t]['yes_sub_title']}" for t in legs),
                "expected_expiration_time": (now + timedelta(hours=rng.uniform(1, 120))).isoformat(),
                "mve_selected_legs": [{"event_ticker": self.leg_markets[t]["event_ticker"],
                                       "market_ticker": t, "side": sides[t]} for t in legs],
            }
            fair = 1.0
            for t in legs:
                b = self.books[t]
                yes_mid = (float(b["yes_dollars"][-1][0]) + 1 - float(b["no_dollars"][-1][0])) / 2
                leg = next(l for l in self.combo_markets[cticker]["mve_selected_legs"] if l["market_ticker"] == t)
                fair *= yes_mid if leg["side"] == "yes" else 1 - yes_mid
            for j in range(rng.choice([1, 1, 2, 4])):
                price = min(0.99, max(0.01, fair * rng.uniform(0.75, 1.25)))
                count = rng.choice([1, 5, 10, 25, 50, 100, 250, 1000, 5000])
                side = "yes" if rng.random() < 0.85 else "no"
                self.trades.append({
                    "trade_id": f"demo-{i}-{j}", "ticker": cticker, "taker_side": side,
                    "taker_outcome_side": side, "yes_price_dollars": f"{price:.4f}",
                    "no_price_dollars": f"{1 - price:.4f}", "count_fp": f"{count:.2f}",
                    "created_time": (now - timedelta(minutes=rng.uniform(0, 180))).isoformat(),
                })

    def combo_trades(self, min_ts: datetime, max_pages: int = 200) -> list[dict]:
        return [t for t in self.trades if datetime.fromisoformat(t["created_time"]) >= min_ts]

    def get_markets(self, tickers: list[str], batch_size: int = 50) -> dict[str, dict]:
        pool = {**self.combo_markets, **self.leg_markets}
        return {t: pool[t] for t in tickers if t in pool}

    def get_events(self, event_tickers: list[str]) -> dict[str, dict]:
        return {t: self.events.get(t, {}) for t in event_tickers}

    def get_orderbooks(self, tickers: list[str]) -> dict[str, dict]:
        return {t: self.books.get(t, {}) for t in tickers}
