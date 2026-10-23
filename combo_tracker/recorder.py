"""The bettor recorder: who places the combo bets that matter, and how they do.

Kalshi's trade feed has no trader ID, but rfq.RfqBook names the requester (a stable
code) behind ~97% of combo bets as they fill. So scripts/recorder_bot.py runs all day
in GitHub Actions (.github/workflows/recorder.yml) and keeps:

- whales: every combo bet of `whale_min` ($5k) or more. Whoever placed one is a whale,
  and from then on their bets of `track_min` ($1k) or more are kept too;
- tennis: every combo bet with a tennis leg of `tennis_min` ($25) or more, so tennis
  combo bettors can be ranked by PnL against the book, with a real sample size;
- tagged: every bet of a bettor you starred in the app, whatever its size.

Each kept bet is priced against its legs' books seconds after it fills (fair value
at leg mids and crossing the spread, as on the Combos page) and settled when its
combo resolves. Settled bets older than a few days are folded into per-day totals
for their code (whales' and tagged bettors' bets stay whole for a year), so the
store stays small however many people bet.

The recorder also looks for a public nickname behind each code (a matched trade
that shows the taker's name in Kalshi's social feed) and saves everything to the
`bettor-data` branch, where the Bettors page reads it. The repo is public and
Kalshi's data isn't ours to republish, so the files there are encrypted with a key
derived from the Kalshi private key (the app and the workflow both have it), or
from BETTOR_DATA_KEY when that's set in both places.

Files on the branch: store.json (everything; the next run picks up from it) and
view.json (the ranked tables and the live bets, small enough to load on a phone).
Tags you star in the app go to the `bettor-tags` branch as tags.json, plain, when
the app has a GitHub token (GH_TOKEN secret); the recorder reads them every minute.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from collections import defaultdict

from .bettors import is_tennis
from .engine import taker_fee
from .rfq import PLACEHOLDER, Match

WHALE_MIN = 5000.0  # a combo bet this big makes its requester a whale
TRACK_MIN = 1000.0  # a whale's later bets this big are kept too
TENNIS_MIN = 25.0  # tennis-leg combo bets this big are kept for everyone
LOCK = 0.95  # a bet at 95¢+ risks a lot to win a little, usually on a combo that's nearly decided
KEEP_DAYS = 365
FOLD_DAYS = 3  # settled bets of ordinary bettors older than this become per-day totals
LIVE_HOURS = 48  # open bets this recent are "live"
MAX_BETS = 400_000
MAX_ROWS = 2000  # bettors in the app's tables
MAX_LIVE = 600
RECENT = 15  # bets shown when a bettor is opened
TOP_LIVE = 150  # tennis bettors whose open bets are shown: the top this many by z and by dollars
DAY = 86400

BRANCH = "bettor-data"
TAGS_BRANCH = "bettor-tags"
STORE_FILE = "store.json"
VIEW_FILE = "view.json"
PROFILES_FILE = "profiles.json"
TAGS_FILE = "tags.json"
MAX_TAGS = 200  # stars in the shared tags.json
REPO = os.environ.get("GITHUB_REPOSITORY") or "capypullup/kalshi-combo-tracker"
RAW = f"https://raw.githubusercontent.com/{REPO}"
VIEW_URL = f"{RAW}/{BRANCH}/{VIEW_FILE}"
PROFILES_URL = f"{RAW}/{BRANCH}/{PROFILES_FILE}"
TAGS_URL = f"{RAW}/{TAGS_BRANCH}/{TAGS_FILE}"

# A kept bet is a dict with short keys (stored as is):
#   id first trade id · c requester code · t time of the first fill · tk combo ticker · s side bought
#   n contracts · p average price of that side · sure every RFQ that fit was theirs · ten has a tennis leg
#   after pricing: fm / fc fair value of the side bought at leg mids / crossing the spread (CRX),
#     L legs, lv a leg's game had started, ti the legs in short, x when the combo should resolve
#   after settling: r "yes", "no" or "void" · lk the social feed was asked about it
FINAL = ("determined", "finalized", "settled", "amended")
# Per-day totals of a code's folded bets, in this order (see Tally).
FIELDS = ("n", "vol", "big", "ev_n", "ev_vol", "ev", "st", "st_vol", "pnl", "wins", "bp", "be", "bv", "live", "ten", "sure")
SCOPES = ("a", "t")  # all bets, tennis bets
PERIODS = {"all": None, "30": 30, "7": 7}


def stake(b: dict) -> float:
    return b["n"] * b["p"]


def is_lock(b: dict) -> bool:
    return b["p"] >= LOCK


def numbers(b: dict) -> tuple[float, float | None, float | None, float | None]:
    """A bet's stake, EV against the book when placed, its variance, and (once settled) PnL, after the taker fee."""
    fee = taker_fee(b["p"], b["n"])
    fair, ev, var, pnl = b.get("fm"), None, None, None
    if fair is not None:
        ev = (fair - b["p"]) * b["n"] - fee
        var = b["n"] ** 2 * fair * (1 - fair)
    if b.get("r") in ("yes", "no"):
        pnl = ((1.0 if b["r"] == b["s"] else 0.0) - b["p"]) * b["n"] - fee
    return stake(b), ev, var, pnl


def short_legs(row: dict, limit: int = 110) -> str:
    """'Lakers · NO Over 220.5 points · Mookie Betts: 2+' from a scored row's legs."""
    parts = [("NO " if l["side"] == "no" else "") + str(l.get("leg") or l.get("ticker") or "")
             for l in row.get("leg_detail") or []]
    text = " · ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def day_of(ts: float) -> int:
    return int(ts // DAY)


def variant(scope: str, lock: bool) -> str:
    return scope + ("L" if lock else "")


class Tally:
    """Totals over some bets: whole ones and folded per-day ones, in the short keys the tables read.

    edge = settled PnL − EV on the bets that are both settled and priced, and z = edge ÷ its
    standard deviation: around 0 is what the book expected, 2+ is hard to get by luck.
    """

    def __init__(self):
        self.s = dict.fromkeys(FIELDS, 0.0)
        self.s["open"] = 0.0
        self.price = 0.0
        self.first = self.last = None

    def add_bet(self, b: dict) -> None:
        size, ev, var, pnl = numbers(b)
        s = self.s
        s["n"] += 1
        s["vol"] += size
        s["big"] = max(s["big"], size)
        s["sure"] += bool(b.get("sure"))
        s["live"] += bool(b.get("lv"))
        s["ten"] += bool(b.get("ten"))
        self.price += b["p"] * size
        self.first = b["t"] if self.first is None else min(self.first, b["t"])
        self.last = b["t"] if self.last is None else max(self.last, b["t"])
        if ev is not None:
            s["ev"] += ev
            s["ev_n"] += 1
            s["ev_vol"] += size
        if pnl is None:
            s["open"] += size
            return
        s["pnl"] += pnl
        s["st"] += 1
        s["st_vol"] += size
        s["wins"] += pnl > 0
        if ev is not None:
            s["bp"] += pnl
            s["be"] += ev
            s["bv"] += var

    def add_bucket(self, day: int, values: list[float]) -> None:
        for k, v in zip(FIELDS, values):
            self.s[k] = max(self.s[k], v) if k == "big" else self.s[k] + v
        self.price += values[FIELDS.index("vol")] * (values[-1] if len(values) > len(FIELDS) else 0)  # avg price × vol
        start, end = day * DAY, (day + 1) * DAY - 1
        self.first = start if self.first is None else min(self.first, start)
        self.last = end if self.last is None else max(self.last, end)

    def bucket(self) -> list[float]:
        """This tally as a stored per-day total (its bets are all settled), with the average price last."""
        vol = self.s["vol"]
        return [round(self.s[k], 4) for k in FIELDS] + [round(self.price / vol, 4) if vol else 0.0]

    def out(self) -> dict:
        s, n = self.s, self.s["n"]
        edge = s["bp"] - s["be"]
        r = lambda x, d=2: None if x is None else round(x, d)  # noqa: E731
        return {
            "n": int(n), "vol": r(s["vol"]), "big": r(s["big"]), "st": int(s["st"]),
            "win": r(s["wins"] / s["st"], 4) if s["st"] else None,
            "pnl": r(s["pnl"]) if s["st"] else None, "pr": r(s["pnl"] / s["st_vol"], 4) if s["st_vol"] else None,
            "ev": r(s["ev"]) if s["ev_n"] else None, "evr": r(s["ev"] / s["ev_vol"], 4) if s["ev_vol"] else None,
            "cov": r(s["ev_n"] / n, 3) if n else None,
            "edge": r(edge) if s["bv"] else None,
            "z": r(edge / math.sqrt(s["bv"]), 2) if s["bv"] > 0 else None,
            "open": r(s["open"]), "avgp": r(self.price / s["vol"], 4) if s["vol"] else None,
            "lv": r(s["live"] / n, 3) if n else None, "ten": int(s["ten"]), "sure": r(s["sure"] / n, 3) if n else None,
            "first": int(self.first * 1000) if self.first else None, "last": int(self.last * 1000) if self.last else None,
        }


def summary(bets: list[dict]) -> dict:
    t = Tally()
    for b in bets:
        t.add_bet(b)
    return t.out()


class Store:
    """Every kept bet, the per-day totals of folded ones, and what we know about each code."""

    def __init__(self, whale_min: float = WHALE_MIN, track_min: float = TRACK_MIN, tennis_min: float = TENNIS_MIN):
        self.whale_min = whale_min
        self.track_min = track_min
        self.tennis_min = tennis_min
        self.bets: dict[str, dict] = {}  # first trade id -> bet
        self.codes: dict[str, dict] = {}  # code -> {"big": biggest bet, "first": first seen, "name": nickname, ...}
        self.buckets: dict[str, dict[str, dict[str, list]]] = {}  # code -> variant -> day -> totals
        self.tags: set[str] = set()  # codes (and nicknames) starred in the app
        self.runs: list[list[float]] = []  # [start, end] of each stretch the recorder was listening

    # ---- recording ----
    def is_whale(self, code: str) -> bool:
        return self.codes.get(code, {}).get("big", 0.0) >= self.whale_min

    def is_tagged(self, code: str) -> bool:
        return code in self.tags or (self.codes.get(code, {}).get("name") or "") in self.tags

    def keeps_whole(self, code: str) -> bool:
        """Whose bets stay whole instead of folding into per-day totals."""
        return self.is_whale(code) or self.is_tagged(code)

    def wants(self, code: str, size: float, tennis: bool) -> bool:
        if code == PLACEHOLDER:
            return False
        if size >= self.whale_min or (size >= self.track_min and self.is_whale(code)):
            return True
        if tennis and size >= self.tennis_min:
            return True
        return self.is_tagged(code)

    def consider(self, m: Match) -> dict | None:
        """Keep this matched bet if it's one we track. Returns the new bet, or None."""
        e = m.execution
        size = e.count * e.price
        if not self.wants(m.creator, size, m.tennis) or e.trade_ids[0] in self.bets:
            return None
        bet = {"id": e.trade_ids[0], "c": m.creator, "t": round(e.ts, 3), "tk": e.ticker, "s": e.side,
               "n": round(e.count, 2), "p": round(e.price, 4), "sure": bool(m.sure), "ten": bool(m.tennis)}
        self.bets[bet["id"]] = bet
        info = self.codes.setdefault(m.creator, {"big": 0.0, "first": e.ts, "name": None})
        info["big"] = max(info["big"], size)
        info["first"] = min(info["first"], e.ts)
        info["last"] = max(info.get("last", 0.0), e.ts)
        return bet

    def price(self, bet: dict, row: dict, now: float | None = None) -> None:
        """Fill in what the Scorer found for this bet seconds after it filled."""
        from .view import is_live

        bet["fm"] = None if row.get("fair_mid") is None else round(row["fair_mid"], 5)
        bet["fc"] = None if row.get("fair_crx") is None else round(row["fair_crx"], 5)
        bet["L"] = row.get("legs")
        bet["lv"] = bool(row.get("leg_detail")) and is_live(row)
        bet["ti"] = short_legs(row)
        if row.get("hours_to_resolve") is not None:
            bet["x"] = round((now or time.time()) + row["hours_to_resolve"] * 3600)
        if not bet.get("ten"):
            bet["ten"] = is_tennis(bet["tk"], [{"event_ticker": l.get("ticker", "")} for l in row.get("leg_detail") or []])

    # ---- settling ----
    def unsettled(self, now: float | None = None, min_age: float = 1800, recheck: float = 6 * 3600) -> list[str]:
        """Combo tickers with open bets worth asking about: past their expected resolution, or not
        asked for a while. Oldest bets first."""
        now = now or time.time()
        due = []
        for b in self.bets.values():
            if b.get("r") is not None or now - b["t"] < min_age:
                continue
            x = b.get("x")
            # not expected to resolve yet: a look every `recheck`; past that time: every `min_age`;
            # never priced (no idea when it resolves): in between
            wait = recheck if x is not None and now < x else min_age if x is not None else 2 * min_age
            if now - b.get("chk", 0.0) < wait:
                continue
            due.append(b)
        due.sort(key=lambda b: b["t"])
        return list(dict.fromkeys(b["tk"] for b in due))

    def settle(self, markets: dict[str, dict], now: float | None = None) -> int:
        """Record results from /markets rows. Returns how many bets settled."""
        now = now or time.time()
        done = 0
        for b in self.bets.values():
            m = markets.get(b["tk"])
            if b.get("r") is not None or m is None:
                continue
            b["chk"] = round(now)
            result, status = m.get("result") or "", m.get("status") or ""
            if result in ("yes", "no") and status in FINAL:
                b["r"] = result
            elif status in ("finalized", "settled") or result == "void":
                b["r"] = "void"  # resolved without a yes/no result: Kalshi refunds it
            else:
                continue
            done += 1
        return done

    # ---- naming ----
    def to_link(self, now: float | None = None, min_age: float = 90, tries: int = 3) -> list[dict]:
        """For each unnamed bettor worth naming (whales, tagged, 3+ tennis bets), one bet to look up in the
        social feed: their biggest not tried yet. Biggest bettors first."""
        now = now or time.time()
        by_code: dict[str, list[dict]] = defaultdict(list)
        whole: dict[str, int] = defaultdict(int)
        for b in self.bets.values():
            whole[b["c"]] += 1
            if not b.get("lk") and now - b["t"] >= min_age:
                by_code[b["c"]].append(b)
        out = []
        for code, bets in by_code.items():
            info = self.codes.get(code) or {}
            if info.get("name") or info.get("tried", 0) >= tries:
                continue
            if not (self.keeps_whole(code) or self.count(code, whole[code]) >= 3):
                continue
            out.append(max(bets, key=stake))
        return sorted(out, key=lambda b: -self.codes[b["c"]]["big"])

    def link(self, bet: dict, trades: list[dict]) -> str | None:
        """Look for the bet's trade in a social-feed page of its market; name its bettor if the taker shows."""
        bet["lk"] = True
        info = self.codes[bet["c"]]
        info["tried"] = info.get("tried", 0) + 1
        hit = next((t for t in trades if t.get("trade_id") == bet["id"]), None)
        name = (hit or {}).get("taker_nickname") or None
        if name:
            info["name"] = name
        return name

    def name(self, code: str, name: str) -> None:
        self.codes.setdefault(code, {"big": 0.0, "first": time.time(), "name": None})["name"] = name

    def code_of(self, name: str) -> str | None:
        return next((c for c, i in self.codes.items() if i.get("name") == name), None)

    def count(self, code: str, whole: int | None = None) -> int:
        """How many bets we have for a code, whole and folded (`whole` when the caller counted already)."""
        if whole is None:
            whole = sum(1 for b in self.bets.values() if b["c"] == code)
        folded = sum(v[0] for v in self.buckets.get(code, {}).get("a", {}).values())
        folded += sum(v[0] for v in self.buckets.get(code, {}).get("aL", {}).values())
        return whole + int(folded)

    # ---- housekeeping ----
    def note_run(self, start: float, now: float) -> None:
        """Mark [start, now] as listened. A run that picks up within 15 minutes of the last continues it."""
        if self.runs and (self.runs[-1][0] == start or start - self.runs[-1][1] <= 900):
            self.runs[-1][1] = max(self.runs[-1][1], now)
        else:
            self.runs.append([start, now])
        self.runs = self.runs[-500:]

    def fold(self, now: float | None = None, fold_days: float = FOLD_DAYS) -> int:
        """Turn old settled bets of ordinary bettors into per-day totals. Returns how many folded."""
        now = now or time.time()
        cutoff = now - fold_days * DAY
        folded = 0
        for b in list(self.bets.values()):
            if b.get("r") is None or b["t"] > cutoff or self.keeps_whole(b["c"]):
                continue
            del self.bets[b["id"]]
            folded += 1
            if b["r"] == "void":
                continue
            day = str(day_of(b["t"]))
            for scope in ("a",) + (("t",) if b.get("ten") else ()):
                key = variant(scope, is_lock(b))
                days = self.buckets.setdefault(b["c"], {}).setdefault(key, {})
                t = Tally()
                if day in days:
                    t.add_bucket(int(day), days[day])
                t.add_bet(b)
                days[day] = t.bucket()
        return folded

    def prune(self, now: float | None = None, keep_days: float = KEEP_DAYS, max_bets: int = MAX_BETS) -> None:
        now = now or time.time()
        bets = sorted(self.bets.values(), key=lambda b: b["t"])
        bets = [b for b in bets if now - b["t"] <= keep_days * DAY][-max_bets:]
        self.bets = {b["id"]: b for b in bets}
        first_day = day_of(now - keep_days * DAY)
        for code in list(self.buckets):
            for key in list(self.buckets[code]):
                self.buckets[code][key] = {d: v for d, v in self.buckets[code][key].items() if int(d) >= first_day}
                if not self.buckets[code][key]:
                    del self.buckets[code][key]
            if not self.buckets[code]:
                del self.buckets[code]
        held = {b["c"] for b in bets} | set(self.buckets)
        self.codes = {c: v for c, v in self.codes.items() if c in held or self.is_tagged(c)}

    # ---- totals ----
    def tally(self, code: str, scope: str = "a", locks: bool = False, since: float | None = None,
              bets=None) -> Tally:
        """Totals for one bettor: whole bets plus folded days, in `scope`, with or without 95¢+ bets, since
        the start of the UTC day of `since` (the day folded totals are kept by, so nothing jumps when bets fold).
        `bets`: this code's whole bets when the caller grouped them already (build_view does, once)."""
        t = Tally()
        first_day = None if since is None else day_of(since)
        for key in (variant(scope, False),) + ((variant(scope, True),) if locks else ()):
            for day, values in self.buckets.get(code, {}).get(key, {}).items():
                if first_day is None or int(day) >= first_day:
                    t.add_bucket(int(day), values)
        for b in self.bets.values() if bets is None else bets:
            if b["c"] != code or b.get("r") == "void" or (first_day is not None and day_of(b["t"]) < first_day):
                continue
            if (scope == "t" and not b.get("ten")) or (not locks and is_lock(b)):
                continue
            t.add_bet(b)
        return t

    # ---- saving ----
    def to_dict(self) -> dict:
        return {"v": 2, "whale_min": self.whale_min, "track_min": self.track_min, "tennis_min": self.tennis_min,
                "runs": self.runs, "tags": sorted(self.tags), "codes": self.codes, "buckets": self.buckets,
                "bets": list(self.bets.values())}

    @classmethod
    def from_dict(cls, d: dict, whale_min: float | None = None, track_min: float | None = None,
                  tennis_min: float | None = None) -> "Store":
        s = cls(whale_min or d.get("whale_min") or WHALE_MIN, track_min or d.get("track_min") or TRACK_MIN,
                tennis_min or d.get("tennis_min") or TENNIS_MIN)
        s.runs = d.get("runs") or []
        s.tags = set(d.get("tags") or [])
        s.codes = d.get("codes") or {}
        s.buckets = d.get("buckets") or {}
        s.bets = {b["id"]: b for b in d.get("bets") or []}
        return s


# ---- the tables the app shows ----
def recent_bet(b: dict) -> dict:
    _, ev, _, pnl = numbers(b)
    return {"id": b["id"], "t": int(b["t"] * 1000), "tk": b["tk"], "s": b["s"], "n": b["n"], "p": b["p"], "fm": b.get("fm"),
            "fc": b.get("fc"), "L": b.get("L"), "lv": b.get("lv"), "ti": b.get("ti"), "r": b.get("r"),
            "ev": None if ev is None else round(ev, 2), "pnl": None if pnl is None else round(pnl, 2)}


def build_view(store: Store, now: float | None = None, max_rows: int = MAX_ROWS, max_live: int = MAX_LIVE) -> dict:
    """The ranked tables for the Bettors page: one row per bettor with totals per scope and period
    (locks in or out for whales), the live (open, recent) bets of the ones worth watching, and the
    recorder's coverage."""
    now = now or time.time()
    by_code: dict[str, list[dict]] = defaultdict(list)
    for b in store.bets.values():
        by_code[b["c"]].append(b)
    codes = set(by_code) | set(store.buckets)
    listed = {c for c in codes
              if store.is_whale(c) or store.is_tagged(c) or store.tally(c, "t", bets=by_code.get(c, ())).s["n"] >= 3}
    vol_of = {c: store.tally(c, bets=by_code.get(c, ())).s["vol"] for c in listed}
    ordered = sorted(listed, key=lambda c: -vol_of[c])[:max_rows]
    rows, by_row = [], {}
    for code in ordered:
        info = store.codes.get(code) or {}
        sums = {}
        mine = by_code.get(code, ())
        for period, days in PERIODS.items():
            since = None if days is None else now - days * DAY
            for scope in SCOPES:
                sums[f"{scope}:{period}"] = store.tally(code, scope, False, since, mine).out()
            if store.is_whale(code):
                sums[f"a+:{period}"] = store.tally(code, "a", True, since, mine).out()
        bets = sorted(mine, key=lambda b: -b["t"])
        row = {"code": code, "name": info.get("name"), "pa": info.get("pa"), "p30": info.get("p30"),
               "seen": int((info.get("first") or now) * 1000), "w": store.is_whale(code), "tg": store.is_tagged(code),
               "big": info.get("big"),  # ever, locks included (the summaries' `big` is per period and setting)
               **sums, "recent": [recent_bet(b) for b in bets[:RECENT]]}
        rows.append(row)
        by_row[code] = row
    # Live: open bets from the last two days of whales, tagged bettors and the top tennis bettors.
    tennis = [r for r in rows if r["t:30"]["n"] >= 5 and r["t:30"]["z"] is not None]
    watch = {r["code"] for r in rows if r["w"] or r["tg"]}
    watch |= {r["code"] for r in sorted(tennis, key=lambda r: -r["t:30"]["z"])[:TOP_LIVE]}
    watch |= {r["code"] for r in sorted(tennis, key=lambda r: -r["t:30"]["vol"])[:TOP_LIVE]}
    live = [b for b in store.bets.values()
            if b["c"] in watch and b.get("r") is None and now - b["t"] <= LIVE_HOURS * 3600]
    live.sort(key=lambda b: -b["t"])
    live_rows = [{**recent_bet(b), "c": b["c"]} for b in live[:max_live]]
    covered = sum(min(e, now) - s for s, e in store.runs if e > s)
    since = store.runs[0][0] if store.runs else None
    return {"v": 2, "saved": int(now * 1000), "since": int(since * 1000) if since else None,
            "coverage": round(covered / (now - since), 3) if since and now > since else None,
            "whale_min": store.whale_min, "track_min": store.track_min, "tennis_min": store.tennis_min, "lock": LOCK,
            "whales": sum(1 for c in codes if store.is_whale(c)), "bettors": len(listed), "bets": len(store.bets),
            "tags": sorted(store.tags), "rows": rows, "live": live_rows}


# ---- encryption ----
class Locked(Exception):
    """The data was saved with a different key than the one we have."""


_FERNETS: dict[bytes, object] = {}  # derived keys, by a hash of what they came from
SALT = b"capyev bettor data"


def _fernet(auth=None, secret: str | None = None):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if secret:
        kind, material = b"pass", secret.encode()
    elif auth is not None:
        kind, material = b"key", auth.key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption())
    else:
        raise Locked("no key to open the bettor data: set KALSHI_PRIVATE_KEY (or BETTOR_DATA_KEY)")
    cache_key = hashlib.sha256(kind + b":" + material).digest()
    f = _FERNETS.get(cache_key)
    if f is None:
        if secret:  # a passphrase someone typed: stretch it (the ciphertext is public), ~0.1 s and 32 MB once
            key = Scrypt(salt=SALT, length=32, n=2 ** 15, r=8, p=1).derive(material)
        else:  # the Kalshi private key is random already
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=b"v1").derive(material)
        f = _FERNETS[cache_key] = Fernet(base64.urlsafe_b64encode(key))
    return f


def key_label(auth=None, secret: str | None = None) -> str:
    """Which key locks the data, safe to show: the Kalshi public key's fingerprint, or "BETTOR_DATA_KEY"."""
    return "BETTOR_DATA_KEY" if secret else auth.fingerprint() if auth is not None else "none"


def seal(data: dict, auth=None, secret: str | None = None) -> bytes:
    raw = gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0)
    token = _fernet(auth, secret).encrypt(raw).decode()
    return json.dumps({"v": 1, "key": key_label(auth, secret), "saved": data.get("saved") or int(time.time() * 1000),
                       "data": token}).encode()


def unseal(blob: bytes, auth=None, secret: str | None = None) -> dict:
    from cryptography.fernet import InvalidToken

    env = json.loads(blob)
    try:
        return json.loads(gzip.decompress(_fernet(auth, secret).decrypt(env["data"].encode())))
    except InvalidToken:
        mine = key_label(auth, secret)
        raise Locked(f"the data is locked with key {env.get('key')}, and this copy has key {mine}") from None


def secret_key() -> str | None:
    return os.environ.get("BETTOR_DATA_KEY") or None


# ---- the data branches (worker side) ----
class GitError(Exception):
    """A fetch or push didn't go through (the message says why, without secrets)."""


def _git(*args: str, data: bytes | None = None, cwd: str | None = None, timeout: float = 60) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "bettor-recorder", "GIT_AUTHOR_EMAIL": "bettor-recorder@users.noreply.github.com",
           "GIT_COMMITTER_NAME": "bettor-recorder", "GIT_COMMITTER_EMAIL": "bettor-recorder@users.noreply.github.com"}
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True, cwd=cwd, env=env,
                          timeout=timeout).stdout.decode().strip()


def _stderr(exc: Exception) -> str:
    err = getattr(exc, "stderr", None) or b""
    return (err.decode(errors="replace") if isinstance(err, bytes) else str(err)).strip()


def fetch_files(remote: str = "origin", branch: str = BRANCH, names=(STORE_FILE,), cwd: str | None = None) -> dict[str, bytes]:
    """The files on a data branch, or {} when the branch doesn't exist yet. Raises GitError when the fetch
    itself fails (network, auth), so a hiccup is never mistaken for "no data yet"."""
    try:
        _git("fetch", "-q", "--depth=1", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}", cwd=cwd)
    except subprocess.CalledProcessError as exc:
        if "couldn't find remote ref" in _stderr(exc):
            return {}
        raise GitError(f"fetching {branch}: {_stderr(exc)[-200:] or 'git failed'}") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"fetching {branch}: timed out") from None
    out = {}
    for name in names:
        try:
            out[name] = subprocess.run(["git", "show", f"{remote}/{branch}:{name}"], capture_output=True, check=True,
                                       cwd=cwd).stdout
        except subprocess.CalledProcessError:
            pass
    return out


def push_files(files: dict[str, bytes], message: str, remote: str = "origin", branch: str = BRANCH,
               cwd: str | None = None) -> str:
    """Replace the data branch with one commit holding `files` (no history, so the branch stays small).
    The push goes through only if the branch is still what this process last fetched or pushed, so two
    recorders can't silently write over each other; otherwise it raises GitError."""
    entries = []
    for name, blob in sorted(files.items()):
        sha = _git("hash-object", "-w", "--stdin", data=blob, cwd=cwd)
        entries.append(f"100644 blob {sha}\t{name}\n")
    tree = _git("mktree", data="".join(entries).encode(), cwd=cwd)
    commit = _git("commit-tree", tree, "-m", message, cwd=cwd)
    tracking = f"refs/remotes/{remote}/{branch}"
    try:
        expect = _git("rev-parse", "--verify", "-q", tracking, cwd=cwd)
    except subprocess.CalledProcessError:
        expect = ""  # never fetched: the branch must not exist yet
    try:
        _git("push", "-q", f"--force-with-lease=refs/heads/{branch}:{expect}", remote, f"{commit}:refs/heads/{branch}",
             cwd=cwd, timeout=120)
    except subprocess.CalledProcessError as exc:
        err = _stderr(exc)
        if "stale info" in err or "rejected" in err:
            raise GitError(f"the {branch} branch changed under us (another recorder writing?); not overwriting it") from None
        raise GitError(f"pushing {branch}: {err[-200:] or 'git failed'}") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"pushing {branch}: timed out") from None
    _git("update-ref", tracking, commit, cwd=cwd)
    return commit


def parse_tags(blob: bytes | str | None) -> set[str]:
    """tags.json: {"tags": ["<code>", "<nickname>", ...]} (or a bare list). Codes are 64 hex digits."""
    if not blob:
        return set()
    try:
        data = json.loads(blob)
    except ValueError:
        return set()
    items = data.get("tags", []) if isinstance(data, dict) else data
    tags = {str(t).strip().lstrip("@#") for t in items if str(t).strip()} if isinstance(items, list) else set()
    return {t for t in tags if valid_tag(t)}


def is_code(tag: str) -> bool:
    return len(tag) == 64 and all(c in "0123456789abcdef" for c in tag)


def valid_tag(tag: str) -> bool:
    """A requester code, or a Kalshi nickname (letters, digits, . _ -)."""
    return is_code(tag) or re.fullmatch(r"[A-Za-z0-9._-]{1,32}", tag) is not None


# ---- the data branch (app side) ----
class Feed:
    """A file from a data branch, fetched from GitHub and (unless plain) opened with the app's key; cached a few minutes."""

    def __init__(self, url: str = VIEW_URL, auth=None, secret: str | None = None, ttl: float = 300.0, plain: bool = False,
                 fetch=None):
        self.url, self.auth, self.secret, self.ttl, self.plain = url, auth, secret, ttl, plain
        self.fetch = fetch or self._fetch
        self.data: dict | None = None
        self.error: str | None = None
        self.fetched_at = 0.0
        self.lock = threading.Lock()

    def _fetch(self) -> bytes | None:
        import requests

        resp = requests.get(self.url, timeout=20, headers={"Cache-Control": "no-cache"})
        if resp.status_code == 404:
            return None  # nothing saved there yet
        resp.raise_for_status()
        return resp.content

    def get(self, force: bool = False) -> dict | None:
        with self.lock:
            if not force and time.time() - self.fetched_at < self.ttl:
                return self.data
            self.fetched_at = time.time()
            try:
                blob = self.fetch()
                if blob is None:
                    self.error = "The recorder hasn't saved anything yet."
                else:
                    self.data = json.loads(blob) if self.plain else unseal(blob, self.auth, self.secret)
                    self.error = None
            except Locked as exc:
                self.error = f"Can't open the bettor data: {exc}."
            except Exception as exc:
                self.error = f"Couldn't load the bettor data: {exc!r}"[:200]
            return self.data


def pack_view(view: dict | None, variant: str = "a:all", open_code: str | None = None, tags=()) -> dict:
    """What ranks.js needs for a table: each bettor with the one summary asked for, plus the open one's bets.

    `variant` is scope (a all bets, t tennis; a+ with 95¢+ bets, whales only) and period, e.g. "t:30".
    `tags` are the app's stars (shared file plus this session); the recorder's own list may be minutes
    old, so it isn't mixed in, or an unstarred bettor would stay starred until the next save.
    """
    rows, detail = [], None
    tags = set(tags)
    for r in (view or {}).get("rows") or []:
        s = r.get(variant) or r.get(variant.replace("+", "")) or r.get("a:all")
        rows.append({"code": r["code"], "name": r.get("name"), "pa": r.get("pa"), "p30": r.get("p30"), "seen": r.get("seen"),
                     "w": r.get("w"), "big": r.get("big"), "tg": r["code"] in tags or (r.get("name") or "") in tags, "s": s})
        if open_code and r["code"] == open_code:
            detail = {"code": open_code, "recent": r.get("recent") or []}
    base = {k: (view or {}).get(k) for k in ("saved", "since", "coverage", "whale_min", "track_min", "tennis_min",
                                            "whales", "bettors", "bets")}
    return {**base, "variant": variant, "rows": rows, "detail": detail}


def pack_live(view: dict | None, tags=()) -> dict:
    """The live bets with each bettor's 30-day record alongside, for the Live tab."""
    tags = set(tags)
    rows = {r["code"]: r for r in (view or {}).get("rows") or []}
    out = []
    for b in (view or {}).get("live") or []:
        r = rows.get(b["c"]) or {}
        out.append({**b, "name": r.get("name"), "w": r.get("w"), "tg": b["c"] in tags or (r.get("name") or "") in tags,
                    "s30": r.get("a:30"), "t30": r.get("t:30")})
    return {"saved": (view or {}).get("saved"), "since": (view or {}).get("since"), "live": out}


class GitHubTags:
    """Tags starred in the app, kept as tags.json on the bettor-tags branch through GitHub's contents API.

    Needs a token with write access to the repo's contents (a fine-grained personal access token for
    this repo only). Without one, tags stay in the phone's browser.
    """

    def __init__(self, token: str, repo: str = REPO, branch: str = TAGS_BRANCH, session=None):
        import requests

        self.api = f"https://api.github.com/repos/{repo}"
        self.branch = branch
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})
        self.lock = threading.Lock()

    def read(self) -> tuple[set[str], str | None]:
        """The tags and the file's sha, or an empty set when the branch or file isn't there yet."""
        resp = self.session.get(f"{self.api}/contents/{TAGS_FILE}", params={"ref": self.branch}, timeout=20)
        if resp.status_code == 404:
            return set(), None
        resp.raise_for_status()
        body = resp.json()
        return parse_tags(base64.b64decode(body.get("content") or "")), body.get("sha")

    def _ensure_branch(self) -> None:
        if self.session.get(f"{self.api}/git/ref/heads/{self.branch}", timeout=20).status_code == 200:
            return
        resp = self.session.get(f"{self.api}", timeout=20)
        resp.raise_for_status()
        default = resp.json()["default_branch"]
        resp = self.session.get(f"{self.api}/git/ref/heads/{default}", timeout=20)
        resp.raise_for_status()
        sha = resp.json()["object"]["sha"]
        resp = self.session.post(f"{self.api}/git/refs", json={"ref": f"refs/heads/{self.branch}", "sha": sha}, timeout=20)
        if resp.status_code not in (201, 422):  # 422: someone just made it
            resp.raise_for_status()

    def toggle(self, tag: str) -> set[str]:
        """Star an unstarred code or nickname, or unstar a starred one. Returns the tags afterwards."""
        return self.set(tag, None)

    def set(self, tag: str, on: bool | None) -> set[str]:
        """Star (on) or unstar (off) one code or nickname; None flips it. Returns the tags afterwards.
        Two taps can't undo each other by accident: the state asked for is what's written."""
        if not valid_tag(tag):
            raise ValueError("a star is a bettor's code or Kalshi nickname")
        with self.lock:
            tags, sha = self.read()
            if on is None:
                on = tag not in tags
            if on == (tag in tags):
                return tags  # already so
            if on and len(tags) >= MAX_TAGS:
                raise ValueError(f"at most {MAX_TAGS} stars; unstar someone first")
            tags.add(tag) if on else tags.discard(tag)
            if sha is None:
                self._ensure_branch()
            content = json.dumps({"tags": sorted(tags)}, indent=1).encode()
            payload = {"message": f"Tags: {'untag' if tag not in tags else 'tag'} {tag[:12]}", "branch": self.branch,
                       "content": base64.b64encode(content).decode()}
            if sha:
                payload["sha"] = sha
            resp = self.session.put(f"{self.api}/contents/{TAGS_FILE}", json=payload, timeout=20)
            resp.raise_for_status()
            return tags
