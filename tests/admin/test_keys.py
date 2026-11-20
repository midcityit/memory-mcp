import re

from memory_mcp.admin.keys import generate_key, hash_secret, parse_key, verify_secret

KEY_RE = re.compile(r"^mt_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{43}$")


def test_generate_key_format():
    full, key_id, secret_hash = generate_key()
    assert KEY_RE.match(full)
    assert len(key_id) == 8
    assert re.fullmatch(r"[0-9a-f]{64}", secret_hash)


def test_parse_key_round_trip():
    full, key_id, secret_hash = generate_key()
    parsed = parse_key(full)
    assert parsed is not None
    pid, secret = parsed
    assert pid == key_id
    assert hash_secret(secret) == secret_hash


def test_parse_key_rejects_non_mt_format():
    assert parse_key("abc") is None
    assert parse_key("mt_short_x") is None
    assert parse_key("") is None


def test_verify_secret():
    full, _, secret_hash = generate_key()
    _, secret = parse_key(full)
    assert verify_secret(secret, secret_hash) is True
    assert verify_secret(secret + "x", secret_hash) is False


def test_generated_keys_differ():
    assert generate_key()[0] != generate_key()[0]
