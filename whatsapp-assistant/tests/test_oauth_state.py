from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from app.utils.oauth_state import OAuthStateCodec, StateError


def _codec(ttl: int = 600) -> OAuthStateCodec:
    return OAuthStateCodec(Fernet.generate_key().decode(), ttl_seconds=ttl)


def test_roundtrip_returns_wa_id() -> None:
    codec = _codec()
    state = codec.encode("34600111222")
    assert codec.decode(state) == "34600111222"


def test_state_is_opaque() -> None:
    codec = _codec()
    state = codec.encode("34600111222")
    # The state must not contain the wa_id verbatim — that would defeat CSRF.
    assert "34600111222" not in state


def test_decode_rejects_tampered_state() -> None:
    codec = _codec()
    state = codec.encode("34600111222")
    tampered = state[:-2] + ("XX" if not state.endswith("XX") else "YY")
    with pytest.raises(StateError):
        codec.decode(tampered)


def test_decode_rejects_state_signed_by_different_key() -> None:
    codec_a = _codec()
    codec_b = _codec()
    state = codec_a.encode("34600111222")
    with pytest.raises(StateError):
        codec_b.decode(state)


def test_decode_rejects_expired_state() -> None:
    codec = _codec(ttl=1)
    state = codec.encode("34600111222")
    time.sleep(2)
    with pytest.raises(StateError):
        codec.decode(state)


def test_encode_rejects_empty_wa_id() -> None:
    codec = _codec()
    with pytest.raises(StateError):
        codec.encode("")


def test_decode_rejects_empty_state() -> None:
    codec = _codec()
    with pytest.raises(StateError):
        codec.decode("")
