import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from combo_tracker.client import KalshiAuth


def pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def body(text: str) -> str:
    """The key text without its -----BEGIN/END----- lines, as people sometimes paste it."""
    return "".join(line for line in text.splitlines() if not line.startswith("-----"))


@pytest.mark.parametrize("make", [ed25519.Ed25519PrivateKey.generate,
                                  lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)])
@pytest.mark.parametrize("shape", ["pem", "escaped", "body"])
def test_signs_with_key_in_any_pasted_shape(make, shape):
    key = make()
    text = {"pem": pem(key), "escaped": pem(key).replace("\n", "\\n"), "body": body(pem(key))}[shape]
    h = KalshiAuth("kid", text).headers("GET", "/trade-api/v2/communications/rfqs")
    message = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/communications/rfqs".encode()
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    if isinstance(key, ed25519.Ed25519PrivateKey):
        key.public_key().verify(sig, message)
    else:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key.public_key().verify(sig, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                           salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    assert h["KALSHI-ACCESS-KEY"] == "kid"


def test_from_env_needs_both_values(monkeypatch):
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    assert KalshiAuth.from_env({"KALSHI_KEY_ID": "kid"}) is None
    key = pem(ed25519.Ed25519PrivateKey.generate())
    assert KalshiAuth.from_env({"KALSHI_KEY_ID": "kid", "KALSHI_PRIVATE_KEY": body(key)}).key_id == "kid"


def test_fingerprint_matches_across_pasted_shapes():
    key = pem(ed25519.Ed25519PrivateKey.generate())
    a, b = KalshiAuth("kid", key), KalshiAuth("kid", body(key))
    assert a.kind == "Ed25519" and len(a.fingerprint()) == 12
    assert a.fingerprint() == b.fingerprint() != KalshiAuth("kid", pem(ed25519.Ed25519PrivateKey.generate())).fingerprint()


def test_batch_orderbooks_fill_gaps_one_by_one(monkeypatch):
    """Kalshi reads a comma-joined `tickers` as one ticker, so books it leaves out are fetched singly."""
    from combo_tracker.client import KalshiClient

    c = KalshiClient(rate_per_sec=1000)
    calls = []

    def fake_get(path, params=None, retries=5, signed=False):
        calls.append((path, params))
        if path == "/markets/orderbooks":
            return {"orderbooks": [{"ticker": "A", "orderbook_fp": {"yes_dollars": [["0.40", "10"]]}},
                                   {"ticker": "A,B", "orderbook_fp": {}}]}
        return {"orderbook_fp": {"yes_dollars": [["0.55", "5"]]}}

    monkeypatch.setattr(c, "_get", fake_get)
    books = c.get_orderbooks(["A", "B", "A"])
    assert sorted(books) == ["A", "B"] and books["B"]["yes_dollars"] == [["0.55", "5"]]
    assert calls[0] == ("/markets/orderbooks", {"tickers": ["A", "B"]}) and calls[1][0] == "/markets/B/orderbook"
