"""One-off probe of Kalshi's public social API (v1). Runs in GitHub Actions."""
import time
from collections import Counter, defaultdict
import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"
ROOT = "https://api.elections.kalshi.com"
API = ROOT + "/v1/social"
V2 = ROOT + "/trade-api/v2"


def get(url, quiet=True, n=400):
    for _ in range(4):
        try:
            r = S.get(url, timeout=25)
        except Exception as e:
            print("ERR", url, e)
            return None
        if r.status_code == 429:
            time.sleep(2)
            continue
        if not quiet or r.status_code != 200:
            print(f"### {r.status_code} {url.replace(ROOT, '')} {r.text[:n]}")
        time.sleep(0.08)
        try:
            return r.json()
        except Exception:
            return None


print("== leaderboard time params ==")
for tp in ["day", "week", "month", "year", "all", "all_time", "daily", "weekly", "monthly", "1d", "7d", "30d", "DAY", "WEEK"]:
    d = get(f"{API}/leaderboard?metric_name=projected_pnl&limit=3&time_period={tp}")
    if d and "rank_list" in d:
        print("time_period", tp, "->", [(r["nickname"], r["value"]) for r in d["rank_list"]])
for sd in [7, 14, 30, 60, 90, 365]:
    d = get(f"{API}/leaderboard?metric_name=projected_pnl&limit=3&since_day_before={sd}")
    print("since_day_before", sd, "->", [(r["nickname"], r["value"]) for r in (d or {}).get("rank_list", [])])
for cat in ["Sports", "Politics", "Crypto", "Economics", "Climate and Weather", "Financials"]:
    d = get(f"{API}/leaderboard?metric_name=projected_pnl&limit=1&category={cat}")
    print("category", cat, len((d or {}).get("rank_list", [])))

print("\n== tennis series feed: last 24h ==")
since = time.time() - 86400
nick_trades = defaultdict(Counter)
for series in ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH",
               "KXATPSETWINNER", "KXATPGAMESPREAD", "KXATPTOTALGAMES"]:
    cursor, n, named, pages, oldest = None, 0, 0, 0, None
    while pages < 150:
        d = get(f"{API}/trades?series_ticker={series}&limit=100" + (f"&cursor={cursor}" if cursor else ""))
        tr = (d or {}).get("trades", [])
        pages += 1
        stop = False
        for t in tr:
            ts = time.mktime(time.strptime(t["create_date"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            if ts < since:
                stop = True
                break
            oldest = t["create_date"]
            n += 1
            for role in ("taker", "maker"):
                nk = t[f"{role}_nickname"]
                if nk:
                    named += 1
                    nick_trades[nk][role] += 1
        cursor = (d or {}).get("cursor")
        if stop or not cursor or not tr:
            break
    print(f"{series}: trades {n} named-sides {named} pages {pages} oldest {oldest}")
print("distinct tennis nicknames:", len(nick_trades))
top = sorted(nick_trades.items(), key=lambda kv: -sum(kv[1].values()))[:40]
for nk, c in top:
    print("  ", nk, dict(c))

print("\n== visibility of top names ==")
vis = Counter()
for nk, _ in top[:25]:
    d = get(f"{API}/trades?nickname={nk}&limit=100")
    m = get(f"{API}/profile/metrics?nickname={nk}")
    tr = (d or {}).get("trades", [])
    vis[(d or {}).get("visibility_state", "visible")] += 1
    print(f"  {nk}: vis={(d or {}).get('visibility_state')} n={len(tr)} cursor={'y' if (d or {}).get('cursor') else 'n'} "
          f"metrics={(m or {}).get('metrics')} oldest={tr[-1]['create_date'] if tr else None} "
          f"series={Counter(t['ticker'].split('-')[0] for t in tr).most_common(3)}")
print(vis)

print("\n== sports leaderboard visibility ==")
lb = get(f"{API}/leaderboard?metric_name=projected_pnl&limit=100&category=Sports") or {}
lbv = Counter()
for r in lb.get("rank_list", [])[:60]:
    d = get(f"{API}/trades?nickname={r['nickname']}&limit=1")
    lbv[(d or {}).get("visibility_state", "visible") + (":anon" if r.get("is_anonymous") else "")] += 1
print(lbv)

print("\n== deep history for one visible tennis name ==")
for nk, _ in top[:25]:
    d = get(f"{API}/trades?nickname={nk}&limit=100")
    if d and d.get("trades") and not d.get("visibility_state"):
        cursor, total, pages = d.get("cursor"), len(d["trades"]), 1
        oldest = d["trades"][-1]["create_date"]
        while cursor and pages < 40:
            d = get(f"{API}/trades?nickname={nk}&limit=100&cursor={cursor}") or {}
            total += len(d.get("trades", []))
            if d.get("trades"):
                oldest = d["trades"][-1]["create_date"]
            cursor, pages = d.get("cursor"), pages + 1
        print(nk, "trades", total, "pages", pages, "oldest", oldest, "more" if cursor else "end")
        break

print("\n== combo trades with names ==")
d = get(f"{API}/trades?series_ticker=KXMVECROSSCATEGORY&limit=100") or {}
print("crosscat named", sum(1 for t in d.get("trades", []) if t["taker_nickname"] or t["maker_nickname"]), "of", len(d.get("trades", [])))
d = get(f"{API}/trades?series_ticker=KXMVESPORTSMULTIGAMEEXTENDED&limit=100") or {}
print("sports multigame named", sum(1 for t in d.get("trades", []) if t["taker_nickname"] or t["maker_nickname"]), "of", len(d.get("trades", [])))
