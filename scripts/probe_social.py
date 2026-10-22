"""One-off probe of Kalshi's public social API (v1). Runs in GitHub Actions."""
import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"
API = "https://api.elections.kalshi.com/v1/social"
N = "weatherman.allday"
SID = "6d479151-102d-4b4b-b881-f86c7c2fc94c"


def show(path, n=1800):
    try:
        r = S.get(API + path, timeout=20)
        print(f"\n### {r.status_code} {path}\n{r.text[:n]}")
        return r
    except Exception as e:
        print(f"\n### ERR {path}: {e}")


for metric in ["projected_pnl", "pnl", "volume", "num_markets_traded", "profit", "realized_pnl"]:
    show(f"/leaderboard?metric_name={metric}&limit=5")
show("/leaderboard?metric_name=projected_pnl&limit=5&category=Sports")
show("/leaderboard?metric_name=projected_pnl&limit=5&since_day_before=7")
show("/leaderboard?metric_name=projected_pnl&limit=200")
for p in [
    f"/profile/trades?nickname={N}&limit=5",
    f"/trades?nickname={N}&limit=5",
    f"/profile/positions?nickname={N}&limit=5",
    f"/profile/positions?nickname={N}&limit=5&status=closed",
    f"/positions?nickname={N}&limit=5",
    f"/profile/top_categories?nickname={N}",
    f"/profile/category_metrics?nickname={N}",
    f"/profile/metrics?nickname={N}&since_day_before=30",
    f"/profile/badges?nickname={N}",
    f"/profile?social_id={SID}",
    f"/trades?social_id={SID}&limit=5",
    f"/profile/trades?social_id={SID}&limit=5",
    "/trades?limit=5",
    "/trades?ticker=KXATPMATCH&limit=5",
    "/market/trades?limit=5",
    "/feed?limit=3",
    "/posts?limit=3",
    f"/posts?nickname={N}&limit=3",
]:
    show(p)
