import hashlib
import hmac
import re
import secrets
import string

_KEY_RE = re.compile(r"^mt_([A-Za-z0-9]{8})_([A-Za-z0-9_-]+)$")
_ID_ALPHABET = string.ascii_letters + string.digits


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, secret_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), secret_hash)


def generate_key() -> tuple[str, str, str]:
    """Return (full_key, key_id, secret_hash)."""
    key_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))
    secret = secrets.token_urlsafe(32)
    return f"mt_{key_id}_{secret}", key_id, hash_secret(secret)


def parse_key(bearer: str) -> tuple[str, str] | None:
    """Return (key_id, secret), or None if bearer is not in mt_ format."""
    m = _KEY_RE.match(bearer)
    return (m.group(1), m.group(2)) if m else None
