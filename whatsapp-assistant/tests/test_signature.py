from __future__ import annotations

import hashlib
import hmac

from app.utils.signature import validate_webhook_signature

SECRET = "meta-secret-test"


def _digest(payload: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_valid_signature_returns_true() -> None:
    payload = b'{"hello":"world"}'
    header = f"sha256={_digest(payload)}"
    assert validate_webhook_signature(payload, header, SECRET) is True


def test_tampered_payload_returns_false() -> None:
    payload = b'{"hello":"world"}'
    header = f"sha256={_digest(payload)}"
    assert validate_webhook_signature(b'{"hello":"WORLD"}', header, SECRET) is False


def test_wrong_secret_returns_false() -> None:
    payload = b"x"
    header = f"sha256={_digest(payload)}"
    assert validate_webhook_signature(payload, header, "different-secret") is False


def test_missing_header_returns_false() -> None:
    assert validate_webhook_signature(b"x", None, SECRET) is False
    assert validate_webhook_signature(b"x", "", SECRET) is False


def test_malformed_header_returns_false() -> None:
    assert validate_webhook_signature(b"x", "deadbeef", SECRET) is False
    assert validate_webhook_signature(b"x", "md5=deadbeef", SECRET) is False
