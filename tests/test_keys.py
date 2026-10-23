from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from combo_tracker.keys import find_secret, kalshi_auth, key_rejected

PEM = ed25519.Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def test_key_at_the_top_works():
    auth, problem = kalshi_auth({"KALSHI_KEY_ID": " kid-e97b ", "KALSHI_PRIVATE_KEY": PEM}, env={})
    assert problem is None and auth.key_id == "kid-e97b"


def test_key_under_a_section_or_in_other_capitals_still_works():
    # Everything after a [section] line in TOML belongs to that section.
    auth, problem = kalshi_auth({"TRADE_PIN": "1234", "alerts": {"KALSHI_KEY_ID": "kid", "kalshi_private_key": PEM}},
                                env={})
    assert problem is None and auth.key_id == "kid"
    assert find_secret({"connections": {"trade_pin": " 99 "}}, "TRADE_PIN", env={}) == "99"


def test_missing_key_says_which_and_never_shows_values():
    auth, problem = kalshi_auth({}, env={})
    assert auth is None and "no secrets yet" in problem
    auth, problem = kalshi_auth({"KALSHI_KEY_ID": "kid-secret-id", "TRADE_PIN": "1234"}, env={})
    assert "no KALSHI_PRIVATE_KEY (it has KALSHI_KEY_ID)" in problem and "kid-secret-id" not in problem
    auth, problem = kalshi_auth({"KALSHI_API_KEY": "kid", "KALSHI_SECRET": PEM}, env={})
    assert "no KALSHI_KEY_ID or KALSHI_PRIVATE_KEY" in problem and "Rename KALSHI_API_KEY, KALSHI_SECRET" in problem
    assert "PRIVATE KEY" not in problem and "1234" not in str(kalshi_auth({"TRADE_PIN": "1234"}, env={}))


def test_blank_values_count_as_missing_and_env_fills_in():
    auth, problem = kalshi_auth({"KALSHI_KEY_ID": "  ", "KALSHI_PRIVATE_KEY": PEM}, env={})
    assert auth is None and "no KALSHI_KEY_ID" in problem
    auth, _ = kalshi_auth({"KALSHI_PRIVATE_KEY": PEM}, env={"KALSHI_KEY_ID": "kid"})
    assert auth.key_id == "kid"


def test_a_broken_key_is_named_without_its_text():
    auth, problem = kalshi_auth({"KALSHI_KEY_ID": "kid", "KALSHI_PRIVATE_KEY": "not a key at all"}, env={})
    assert auth is None and problem.startswith("KALSHI_PRIVATE_KEY couldn't be read") and "not a key" not in problem


def test_a_key_is_parsed_once_and_a_new_key_gives_a_new_auth():
    secrets = {"KALSHI_KEY_ID": "kid", "KALSHI_PRIVATE_KEY": PEM}
    first, _ = kalshi_auth(secrets, env={})
    assert kalshi_auth(dict(secrets), env={})[0] is first
    assert kalshi_auth({**secrets, "KALSHI_KEY_ID": "kid2"}, env={})[0] is not first


def test_key_rejected_spots_kalshi_turning_the_key_down():
    assert key_rejected("stream: WebSocketBadStatusException('Handshake status 401 Unauthorized -+-+- {}')")
    assert key_rejected("matcher: HTTPError('401 Client Error: Unauthorized for url: ...')")
    assert key_rejected('{"error": {"code": "INCORRECT_API_KEY_SIGNATURE"}}')
    assert not key_rejected("stream: {'code': 25, 'msg': 'Subscription buffer overflow'}")
    assert not key_rejected("stream: ConnectionResetError(104, 'Connection reset by peer')") and not key_rejected(None)
