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


# ---- public profiles (the Bettors page) ----
PLAYERS = ["SIN", "ALC", "ZVE", "MED", "RUU", "FRI", "DEM", "TSI", "PAU", "SHE", "SAB", "GAU", "PEG", "RYB",
           "KEY", "NAV", "PAO", "JOV", "AND", "MUC", "FON", "MEN", "LEH", "BON"]
BETTORS = [  # nickname, style, trades hidden
    ("baseline.sharp", "sharp", False), ("Hughman", "sharp", False), ("JohnR0809", "maker", False),
    ("clay.court.kid", "sharp", False), ("dry.ear", "square", False), ("glass.cup", "square", False),
    ("long.jet", "square", False), ("tall.van", "square", False), ("netcord", "maker", False),
    ("tiebreak.tom", "square", False), ("ace.pilot", "sharp", False), ("CookieV222", "square", False),
    ("BigWhale", "square", True), ("Blackbriar", "maker", True), ("4thewin", "sharp", True),
]


class _DemoSocial:
    def __init__(self, seed: int = 11):
        rng = random.Random(seed)
        now = datetime.now(timezone.utc).timestamp()
        self.markets: dict[str, dict] = {}
        self.truth: dict[str, tuple[float, float, float]] = {}  # ticker -> (true p, book mid, spread)
        for i in range(0, len(PLAYERS) - 1, 2):
            for day, series in ((0, "KXATPMATCH"), (1, "KXWTAMATCH"), (3, "KXATPCHALLENGERMATCH"), (6, "KXITFMATCH")):
                a, b = PLAYERS[i], PLAYERS[i + 1]
                event = f"{series}-26OCT{20 - day:02d}{a}{b}"
                q = rng.uniform(0.2, 0.8)
                mid = min(0.95, max(0.05, q + rng.gauss(0, 0.06)))
                spread = rng.choice([0.01, 0.02, 0.02, 0.03])
                settled = day > 0 or rng.random() < 0.5
                result = ("yes" if rng.random() < q else "no") if settled else None
                for who, p_yes in ((a, mid), (b, 1 - mid)):
                    ticker = f"{event}-{who}"
                    self.markets[ticker] = {
                        "ticker": ticker, "event_ticker": event, "yes_sub_title": f"{who.title()} wins",
                        "status": "finalized" if settled else "active",
                        "result": "" if result is None else (result if who == a else ("no" if result == "yes" else "yes")),
                        "_t0": now - day * 86400 - rng.uniform(1, 8) * 3600,
                    }
                    self.truth[ticker] = (q if who == a else 1 - q, p_yes, spread)
        self.trades: list[dict] = []
        tickers = list(self.markets)
        n = 0
        for nick, style, _ in BETTORS + [("", "square", False)] * 6:
            for _ in range(rng.randint(40, 160)):
                tk = rng.choice(tickers)
                q, mid, spread = self.truth[tk]
                t = self.markets[tk]["_t0"] + rng.uniform(0, 3 * 3600)
                if t > now:
                    continue
                if style == "sharp":
                    side = "yes" if q > mid else "no"
                elif style == "maker":
                    side = rng.choice(["yes", "no"])
                else:
                    side = rng.choice(["yes", "no"])
                taker = style != "maker"
                # Takers pay the ask; makers get filled at their bid.
                ask_yes, bid_yes = mid + spread / 2, mid - spread / 2
                if taker:
                    yes_price = ask_yes if side == "yes" else bid_yes
                else:
                    yes_price = bid_yes if side == "yes" else ask_yes
                count = rng.choice([10, 25, 50, 100, 200, 500, 1000])
                other = rng.choice([x[0] for x in BETTORS if not x[2]] + [""] * 8)
                n += 1
                self.trades.append({
                    "trade_id": f"demo-social-{n}", "ticker": tk, "price_dollars": f"{yes_price:.4f}",
                    "count_fp": f"{count:.2f}", "create_date": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                    "taker_side": side if taker else ("no" if side == "yes" else "yes"), "taker_action": "buy",
                    "taker_nickname": nick if taker else other, "maker_nickname": other if taker else nick,
                })
        self.trades.sort(key=lambda x: x["create_date"], reverse=True)
        self.hidden = {n for n, _, h in BETTORS if h}
        self.rng = rng

    def _visible(self, t: dict) -> dict:
        return {**t, "taker_nickname": "" if t["taker_nickname"] in self.hidden else t["taker_nickname"],
                "maker_nickname": "" if t["maker_nickname"] in self.hidden else t["maker_nickname"]}

    def social_trades(self, nickname=None, series_ticker=None, cursor=None, limit=100) -> dict:
        if nickname in self.hidden:
            return {"trades": [], "visibility_state": "hidden"}
        rows = [self._visible(t) for t in self.trades
                if (not series_ticker or t["ticker"].startswith(series_ticker + "-"))
                and (not nickname or nickname in (t["taker_nickname"], t["maker_nickname"]))]
        start = int(cursor or 0)
        page = rows[start : start + limit]
        return {"trades": page, "cursor": str(start + limit) if start + limit < len(rows) else ""}

    def profile_metrics(self, nickname: str, days: int | None = None) -> dict:
        h = sum(map(ord, nickname))
        style = next((s for n, s, _ in BETTORS if n == nickname), "square")
        base = {"sharp": 90_000, "maker": 250_000, "square": -20_000}[style] * (1 + h % 7)
        scale = 0.15 if days else 1
        return {"pnl": int(base * scale * 10_000), "num_markets_traded": int((300 + h * 3) * (0.2 if days else 1))}

    def leaderboard(self, metric="projected_pnl", limit=100, category=None, days=None) -> list[dict]:
        names = ["Blackbriar", "4thewin", "BigWhale", "baseline.sharp", "JohnR0809"]
        rows = sorted(names, key=lambda n: -self.profile_metrics(n, days)["pnl"])
        return [{"nickname": n, "rank": i + 1, "value": self.profile_metrics(n, days)["pnl"] / 10_000, "is_anonymous": False}
                for i, n in enumerate(rows[:limit])]

    def get_candles(self, tickers: list[str], start_ts: int, end_ts: int) -> dict[str, list[dict]]:
        out = {}
        for tk in tickers:
            if tk not in self.truth:
                continue
            _, mid, spread = self.truth[tk]
            bid, ask = f"{mid - spread / 2:.4f}", f"{mid + spread / 2:.4f}"
            out[tk] = [{"end_period_ts": t, "yes_bid": {"open_dollars": bid, "close_dollars": bid},
                        "yes_ask": {"open_dollars": ask, "close_dollars": ask}}
                       for t in range(start_ts - start_ts % 60 + 60, end_ts + 60, 60)]
        return out


def _social(self) -> _DemoSocial:
    if not hasattr(self, "_social_data"):
        self._social_data = _DemoSocial()
    return self._social_data


_combo_get_markets = DemoClient.get_markets


def _get_markets(self, tickers: list[str], batch_size: int = 50) -> dict[str, dict]:
    out = _combo_get_markets(self, tickers, batch_size)
    social = _social(self).markets
    out.update({t: social[t] for t in tickers if t in social})
    return out


DemoClient.get_markets = _get_markets
for _name in ("social_trades", "profile_metrics", "leaderboard", "get_candles"):
    setattr(DemoClient, _name, (lambda name: lambda self, *a, **k: getattr(_social(self), name)(*a, **k))(_name))
