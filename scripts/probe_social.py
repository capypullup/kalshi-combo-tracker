"""One-off probe: find Kalshi's social/profile/leaderboard endpoints and check RFQ access.

Runs in GitHub Actions (the dev sandbox can't reach Kalshi). Prints what it finds.
"""
import re
import sys
import json
import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
API = "https://api.elections.kalshi.com"


def show(url, n=1500, **kw):
    try:
        r = S.get(url, timeout=20, **kw)
        body = r.text
        print(f"\n### {r.status_code} {url}\n{body[:n]}")
        return r
    except Exception as e:
        print(f"\n### ERR {url}: {e}")


paths = set()
for page in ["https://kalshi.com/", "https://kalshi.com/leaderboard", "https://kalshi.com/profile/weatherman.allday"]:
    r = show(page, n=300)
    if not r:
        continue
    scripts = set(re.findall(r'src="([^"]+\.js[^"]*)"', r.text))
    print("scripts:", len(scripts))
    for s in list(scripts)[:80]:
        url = s if s.startswith("http") else "https://kalshi.com" + s
        try:
            js = S.get(url, timeout=20).text
        except Exception:
            continue
        for m in re.findall(r'["\'`](/?(?:v1|trade-api/v2)?/?(?:social|leaderboard|profile|profiles|users?)[^"\'`\s]{0,80})["\'`]', js):
            paths.add(m)
        for m in re.findall(r'[^"\'`\s]{0,40}leaderboard[^"\'`\s]{0,60}', js):
            paths.add("LB:" + m)
print("\n=== candidate paths from bundles ===")
for p in sorted(paths)[:400]:
    print(p)

print("\n=== direct guesses ===")
for p in [
    "/v1/social/profile?nickname=weatherman.allday",
    "/v1/social/profiles/weatherman.allday",
    "/v1/social/leaderboard",
    "/v1/social/leaderboard?metric=pnl",
    "/v1/leaderboard",
    "/v1/social/profile/metrics?nickname=weatherman.allday",
    "/trade-api/v2/communications/rfqs?limit=5",
    "/trade-api/v2/communications/quotes?limit=5",
]:
    show(API + p)
