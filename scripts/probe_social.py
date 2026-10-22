"""One-off probe of Kalshi's public social API (v1). Runs in GitHub Actions."""
import time
from collections import Counter
import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"
ROOT = "https://api.elections.kalshi.com"
API = ROOT + "/v1/social"
V2 = ROOT + "/trade-api/v2"


def get(url, quiet=True, n=500):
    for _ in range(4):
        r = S.get(url, timeout=25)
        if r.status_code == 429:
            time.sleep(2)
            continue
        if not quiet or r.status_code != 200:
            print(f"### {r.status_code} {url.replace(ROOT, '')[:200]} {r.text[:n]}")
        time.sleep(0.08)
        try:
            return r.json()
        except Exception:
            return None


print("== side/action semantics over 3000 trades ==")
soc, cursor = [], None
for _ in range(30):
    d = get(f"{API}/trades?limit=100" + (f"&cursor={cursor}" if cursor else "")) or {}
    soc += d.get("trades", [])
    cursor = d.get("cursor")
v2, cursor = {}, None
for _ in range(6):
    d = get(f"{V2}/markets/trades?limit=1000" + (f"&cursor={cursor}" if cursor else "")) or {}
    for t in d.get("trades", []):
        v2[t["trade_id"]] = t
    cursor = d.get("cursor")
combos = Counter()
for t in soc:
    o = v2.get(t["trade_id"])
    if o:
        combos[(t["taker_side"], t["taker_action"], o.get("taker_side"), o.get("taker_outcome_side"), o.get("taker_book_side"),
                abs(float(t["price_dollars"]) - float(o["yes_price_dollars"])) < 1e-6)] += 1
for k, v in combos.most_common():
    print("social(side,action) v2(side,outcome,book) price==yes:", k, v)
print("social keys:", sorted(soc[0].keys()) if soc else None)

print("\n== a visible tennis bettor's history depth ==")
for nk in ["Hughman", "JohnR0809", "dry.ear"]:
    cursor, total, pages, oldest, roles = None, 0, 0, None, Counter()
    while pages < 30:
        d = get(f"{API}/trades?nickname={nk}&limit=100" + (f"&cursor={cursor}" if cursor else "")) or {}
        tr = d.get("trades", [])
        total += len(tr)
        for t in tr:
            roles["taker" if t["taker_nickname"] == nk else "maker" if t["maker_nickname"] == nk else "neither"] += 1
        if tr:
            oldest = tr[-1]["create_date"]
        cursor, pages = d.get("cursor"), pages + 1
        if not cursor or not tr:
            break
    print(nk, "trades", total, "pages", pages, "oldest", oldest, "more" if cursor else "end", dict(roles))
d = get(f"{API}/trades?nickname=Hughman&limit=100&min_ts={int(time.time()) - 86400 * 20}&max_ts={int(time.time()) - 86400 * 10}") or {}
tr = d.get("trades", [])
print("min/max_ts on nickname feed:", len(tr), tr[0]["create_date"] if tr else None, tr[-1]["create_date"] if tr else None)

print("\n== candle batch limits ==")
tk = list(dict.fromkeys(t["ticker"] for t in soc if not t["ticker"].startswith("KXMVE")))
now = int(time.time())
for n, minutes in [(10, 60), (50, 60), (100, 60), (100, 600), (20, 1440)]:
    d = get(f"{V2}/markets/candlesticks?market_tickers={','.join(tk[:n])}&start_ts={now - minutes * 60}&end_ts={now}&period_interval=1", n=300)
    if d and "markets" in d:
        print(f"tickers={n} minutes={minutes}: markets {len(d['markets'])} candles {sum(len(m.get('candlesticks', [])) for m in d['markets'])} keys {sorted(d['markets'][0].keys())}")
combo = next((t["ticker"] for t in soc if t["ticker"].startswith("KXMVE")), None)
if combo:
    d = get(f"{V2}/markets/candlesticks?market_tickers={combo}&start_ts={now - 3600}&end_ts={now}&period_interval=1", n=600, quiet=False)
d = get(f"{V2}/markets/candlesticks?market_tickers={tk[0]}&start_ts={now - 86400 * 9}&end_ts={now - 86400 * 8}&period_interval=1", n=300, quiet=False)

print("\n== settled market fields ==")
d = get(f"{V2}/markets?status=settled&limit=2&series_ticker=KXWTAMATCH") or {}
for m in d.get("markets", []):
    print({k: m.get(k) for k in ("ticker", "status", "result", "settlement_value_dollars", "close_time", "settlement_ts", "last_price_dollars", "expiration_value")})
d = get(f"{V2}/markets?tickers=" + ",".join(m["ticker"] for m in d.get("markets", [])), quiet=True) or {}
print("tickers= lookup returns status:", [(m["ticker"], m["status"], m.get("result")) for m in d.get("markets", [])])

print("\n== metrics variants ==")
for q in ["nickname=Hughman", "nickname=Hughman&since_day_before=30", "nickname=Blackbriar", "nickname=Blackbriar&since_day_before=30"]:
    get(f"{API}/profile/metrics?{q}", quiet=False)
get(f"{API}/leaderboard?metric_name=projected_pnl&limit=3&category=Sports&since_day_before=30", quiet=False)
