"""One-off probe of Kalshi's public social API (v1). Runs in GitHub Actions."""
import json
import time
from collections import Counter
import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"
ROOT = "https://api.elections.kalshi.com"
API = ROOT + "/v1/social"
V2 = ROOT + "/trade-api/v2"


def get(url, n=900, quiet=False):
    try:
        r = S.get(url, timeout=25)
    except Exception as e:
        print(f"\n### ERR {url}: {e}")
        return None
    if not quiet:
        print(f"\n### {r.status_code} {url.replace(ROOT, '')}\n{r.text[:n]}")
    try:
        return r.json()
    except Exception:
        return None


def keys(d):
    return {k: (type(v).__name__, (v if not isinstance(v, (list, dict)) else len(v))) for k, v in (d or {}).items()}


print("== leaderboard params ==")
for q in ["limit=100", "limit=100&offset=100", "limit=100&page=2", "limit=50&cursor=x",
          "limit=5&category=Tennis", "limit=5&category=tennis", "limit=5&category=Sports&time_period=week",
          "limit=5&period=week", "limit=5&since_day_before=30", "limit=5&since_day_before=1",
          "limit=5&time_frame=7d", "limit=5&metric_name=volume&category=Sports"]:
    d = get(f"{API}/leaderboard?metric_name=projected_pnl&{q}" if "metric_name" not in q else f"{API}/leaderboard?{q}", n=300)
    if d and "rank_list" in d:
        rl = d["rank_list"]
        print("  n=", len(rl), "first/last rank", rl[0]["rank"] if rl else None, rl[-1]["rank"] if rl else None,
              "top keys", keys(d))
lb = get(f"{API}/leaderboard?metric_name=projected_pnl&limit=100&category=Sports", quiet=True) or {}
names = [r["nickname"] for r in lb.get("rank_list", [])]
print("\nsports top 100:", names)
for r in lb.get("rank_list", [])[:3]:
    m = get(f"{API}/profile/metrics?nickname={r['nickname']}", n=400)
    print("  leaderboard value", r["value"], "vs metrics", m)

print("\n== social trades params ==")
d = get(f"{API}/trades?limit=3", n=200)
print("top keys:", keys(d))
for q in ["limit=1000", "limit=500", "limit=100&cursor=abc", "limit=5&ticker=KXNBAGAME-26OCT21SACLAC-LAC",
          "limit=5&market_ticker=KXNBAGAME-26OCT21SACLAC-LAC", "limit=5&event_ticker=KXNBAGAME-26OCT21SACLAC",
          "limit=5&series_ticker=KXATPMATCH", f"limit=5&min_ts={int(time.time()) - 86400 * 3}&max_ts={int(time.time()) - 86400 * 2}"]:
    d = get(f"{API}/trades?{q}", n=250)
    if d and d.get("trades"):
        tr = d["trades"]
        print("  n=", len(tr), "keys", keys(d), "tickers", Counter(t["ticker"].split("-")[0] for t in tr).most_common(4),
              "dates", tr[0]["create_date"], tr[-1]["create_date"])

print("\n== per-profile history depth / combos / tennis ==")
for nick in names[:6]:
    d = get(f"{API}/trades?nickname={nick}&limit=1000", quiet=True) or {}
    tr = d.get("trades", [])
    roles = Counter("taker" if t["taker_nickname"] == nick else "maker" if t["maker_nickname"] == nick else "?" for t in tr)
    print(nick, "n", len(tr), "keys", keys(d), roles, Counter(t["ticker"].split("-")[0] for t in tr).most_common(6),
          (tr[0]["create_date"], tr[-1]["create_date"]) if tr else None)
    if d.get("cursor"):
        d2 = get(f"{API}/trades?nickname={nick}&limit=1000&cursor={d['cursor']}", quiet=True) or {}
        t2 = d2.get("trades", [])
        print("   page2", len(t2), (t2[0]["create_date"], t2[-1]["create_date"]) if t2 else None)

print("\n== price semantics: compare social vs v2 trades by trade_id ==")
v2 = get(f"{V2}/markets/trades?limit=1000", quiet=True) or {}
v2map = {t["trade_id"]: t for t in v2.get("trades", [])}
soc = get(f"{API}/trades?limit=1000", quiet=True) or {}
shown = 0
for t in soc.get("trades", []):
    o = v2map.get(t["trade_id"])
    if o and shown < 12:
        shown += 1
        print({k: t[k] for k in ("ticker", "price", "count_fp", "taker_side", "taker_action")},
              {k: o.get(k) for k in ("yes_price_dollars", "no_price_dollars", "count_fp", "taker_side", "taker_outcome_side", "taker_book_side")})
print("overlap", sum(1 for t in soc.get("trades", []) if t["trade_id"] in v2map), "of", len(soc.get("trades", [])))
named = [t for t in soc.get("trades", []) if t["taker_nickname"] or t["maker_nickname"]]
print("named share in global feed:", len(named), "/", len(soc.get("trades", [])),
      "taker named", sum(1 for t in named if t["taker_nickname"]), "combo named", sum(1 for t in named if t["ticker"].startswith("KXMVE")))

print("\n== candlesticks ==")
tk = next((t["ticker"] for t in soc.get("trades", []) if not t["ticker"].startswith("KXMVE")), None)
now = int(time.time())
if tk:
    series = tk.split("-")[0]
    get(f"{V2}/series/{series}/markets/{tk}/candlesticks?start_ts={now - 3600}&end_ts={now}&period_interval=1", n=1200)
    get(f"{V2}/markets/candlesticks?market_tickers={tk}&start_ts={now - 3600}&end_ts={now}&period_interval=1", n=1200)
    m = get(f"{V2}/markets/{tk}", n=0, quiet=True) or {}
    print("market keys:", sorted((m.get("market") or {}).keys()))
settled = get(f"{V2}/markets?status=settled&limit=1&series_ticker=KXATPMATCH", n=2500)
