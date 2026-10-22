import requests
U = "https://api.elections.kalshi.com/v1/social/leaderboard?metric_name=projected_pnl&category=Sports&limit="
for ua in [None, "Mozilla/5.0"]:
    for lim in (5, 10, 100):
        h = {"User-Agent": ua} if ua else {}
        r = requests.get(U + str(lim), headers=h, timeout=20)
        print(ua or "python-requests", lim, [x["nickname"] for x in r.json().get("rank_list", [])[:5]])
