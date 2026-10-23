"""The app's Kalshi API key and TRADE_PIN from Streamlit secrets, and what's wrong when they
can't be used, in words that say what to fix."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .client import KalshiAuth

KEY_ID, PRIVATE_KEY = "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY"


def _value(v) -> str | None:
    if v is None or isinstance(v, Mapping):
        return None
    return str(v).strip() or None


def find_secret(secrets: Mapping | None, name: str, env: Mapping | None = None) -> str | None:
    """`name` from the secrets or the environment.

    Secrets are TOML, where every line after a [section] header belongs to that section, and
    names are case-sensitive. Both are easy to get wrong when pasting, so the name in other
    capitals, or under a [section], still counts.
    """
    secrets = secrets or {}
    env = os.environ if env is None else env
    found = _value(secrets.get(name)) or _value(env.get(name))
    if found:
        return found
    tables = [secrets] + [t for t in secrets.values() if isinstance(t, Mapping)]
    for table in tables:
        for k, v in table.items():
            if isinstance(k, str) and k.strip().upper() == name and _value(v):
                return _value(v)
    return None


def _near_misses(secrets: Mapping) -> list[str]:
    """Secret names that mention Kalshi but aren't the two the app reads (names only, never values)."""
    out = []
    for k, v in secrets.items():
        if isinstance(v, Mapping):
            out += [f"[{k}] {n}" for n in v if isinstance(n, str) and "KALSHI" in n.upper()
                    and n.strip().upper() not in (KEY_ID, PRIVATE_KEY)]
        elif isinstance(k, str) and "KALSHI" in k.upper() and k.strip().upper() not in (KEY_ID, PRIVATE_KEY):
            out.append(k)
    return out[:4]


def kalshi_auth(secrets: Mapping | None, env: Mapping | None = None) -> tuple[KalshiAuth | None, str | None]:
    """The API key, or None and why, as a phrase that reads after "RFQ matching is off: "."""
    key_id, key = find_secret(secrets, KEY_ID, env), find_secret(secrets, PRIVATE_KEY, env)
    if not key_id or not key:
        missing = " or ".join(n for n, v in ((KEY_ID, key_id), (PRIVATE_KEY, key)) if not v)
        if not secrets and not key_id and not key:
            return None, f"the app has no secrets yet. Add {KEY_ID} and {PRIVATE_KEY} in Manage app → Settings → Secrets"
        have = f" (it has {KEY_ID if key_id else PRIVATE_KEY})" if key_id or key else ""
        odd = _near_misses(secrets or {})
        hint = f". Rename {', '.join(odd)} to match" if odd else ""
        return None, f"the app's secrets have no {missing}{have}{hint}"
    try:
        return KalshiAuth(key_id, key), None
    except Exception as exc:  # the parsers' messages never include the key itself
        return None, (f"{PRIVATE_KEY} couldn't be read as a private key ({type(exc).__name__}). Paste the whole key "
                      f"file, -----BEGIN and -----END lines included")
