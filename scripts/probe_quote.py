"""Probe: Kalshi's public OpenAPI spec for RFQ/quote endpoints, and what a combo market shows now.

Prints only public data. For the signed calls it prints the HTTP status and the
field names, never values, since this output lands on a public branch.
"""
import json
import os
import sys
from collections import Counter

import requests
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from combo_tracker.client import BASE_URL, KalshiAuth, KalshiClient  # noqa: E402

spec = None
for url in ("https://docs.kalshi.com/openapi.yaml", "https://docs.kalshi.com/openapi.json",
            "https://trading-api.readme.io/openapi.json"):
    try:
        r = requests.get(url, timeout=20)
        print(url, r.status_code, len(r.content))
        if r.ok:
            spec = r.json() if url.endswith("json") else yaml.safe_load(r.text)
            break
    except Exception as e:
        print(url, "ERR", e)

if spec:
    for path, ops in spec.get("paths", {}).items():
        if "communications" in path or "multivariate" in path:
            for m, op in ops.items():
                if isinstance(op, dict):
                    print(f"\n### {m.upper()} {path}: {op.get('summary')}")
                    for p in op.get("parameters", []):
                        print("  param", json.dumps(p)[:300])
                    if op.get("requestBody"):
                        print("  body", json.dumps(op["requestBody"])[:600])
    for name, s in spec.get("components", {}).get("schemas", {}).items():
        if any(k in name.lower() for k in ("quote", "rfq", "multivariate")):
            print(f"\n## schema {name}\n", json.dumps(s)[:2500])

c = KalshiClient(rate_per_sec=4)
trades, cursor = [], None
for _ in range(15):
    d = c._get("/markets/trades", {"limit": 1000, **({"cursor": cursor} if cursor else {})})
    trades += [t for t in d.get("trades", []) if t["ticker"].startswith("KXMVE")]
    cursor = d.get("cursor")
    if not cursor:
        break
print("\ncombo trades sampled:", len(trades))
cnt = Counter(t["ticker"] for t in trades)
print("repeat tickers:", cnt.most_common(5))
if cnt:
    tick = cnt.most_common(1)[0][0]
    m = c._get("/markets", {"tickers": tick})["markets"][0]
    print("combo market:", json.dumps({k: v for k, v in m.items() if not k.startswith("rules")})[:3000])
    try:
        print("combo orderbook:", json.dumps(c._get(f"/markets/{tick}/orderbook"))[:800])
    except Exception as e:
        print("combo orderbook ERR", e)

auth = KalshiAuth.from_env()
print("\nAPI key set:", auth is not None)
if auth:
    s = requests.Session()
    for path, params in (("/communications/id", None), ("/communications/quotes", {"limit": 1}),
                         ("/communications/rfqs", {"limit": 1})):
        r = s.get(BASE_URL + path, params=params, headers=auth.headers("GET", "/trade-api/v2" + path), timeout=20)
        try:
            body = r.json()
            shape = {k: (sorted(v[0].keys()) if isinstance(v, list) and v and isinstance(v[0], dict) else type(v).__name__)
                     for k, v in body.items()}
        except Exception:
            shape = r.text[:200]
        print(path, r.status_code, shape)
