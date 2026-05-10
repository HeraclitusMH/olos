from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.utils.encryption import TokenEncryption, TokenEncryptionError
from app.utils.generate_key import generate_key


def test_roundtrip_recovers_plaintext() -> None:
    enc = TokenEncryption(Fernet.generate_key().decode())
    ciphertext = enc.encrypt("ya29.access-token-value")
    assert isinstance(ciphertext, bytes)
    assert b"ya29" not in ciphertext  # not stored in plaintext
    assert enc.decrypt(ciphertext) == "ya29.access-token-value"


def test_ciphertext_differs_per_call() -> None:
    enc = TokenEncryption(Fernet.generate_key().decode())
    a = enc.encrypt("same-token")
    b = enc.encrypt("same-token")
    assert a != b  # Fernet uses a random IV
    assert enc.decrypt(a) == enc.decrypt(b) == "same-token"


def test_decrypt_rejects_corrupted_ciphertext() -> None:
    enc = TokenEncryption(Fernet.generate_key().decode())
    ciphertext = enc.encrypt("token")
    corrupted = ciphertext[:-2] + b"AA"
    with pytest.raises(TokenEncryptionError):
        enc.decrypt(corrupted)


def test_decrypt_rejects_token_from_different_key() -> None:
    enc1 = TokenEncryption(Fernet.generate_key().decode())
    enc2 = TokenEncryption(Fernet.generate_key().decode())
    ciphertext = enc1.encrypt("secret")
    with pytest.raises(TokenEncryptionError):
        enc2.decrypt(ciphertext)


def test_init_rejects_empty_key() -> None:
    with pytest.raises(TokenEncryptionError):
        TokenEncryption("")


def test_init_rejects_invalid_key() -> None:
    with pytest.raises(TokenEncryptionError):
        TokenEncryption("not-a-fernet-key")


def test_encrypt_rejects_empty_plaintext() -> None:
    enc = TokenEncryption(Fernet.generate_key().decode())
    with pytest.raises(TokenEncryptionError):
        enc.encrypt("")


def test_decrypt_rejects_empty_ciphertext() -> None:
    enc = TokenEncryption(Fernet.generate_key().decode())
    with pytest.raises(TokenEncryptionError):
        enc.decrypt(b"")


def test_generate_key_returns_valid_fernet_key() -> None:
    key = generate_key()
    assert isinstance(key, str)
    enc = TokenEncryption(key)
    assert enc.decrypt(enc.encrypt("hello")) == "hello"
