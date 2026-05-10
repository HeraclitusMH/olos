"""Symmetric token encryption using Fernet (AES-128-CBC + HMAC-SHA256)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionError(Exception):
    """Raised when encryption or decryption fails."""


class TokenEncryption:
    """Wraps :class:`cryptography.fernet.Fernet` for storing OAuth tokens.

    The key must be a urlsafe-base64-encoded 32-byte string (the format
    produced by :func:`cryptography.fernet.Fernet.generate_key`).
    """

    def __init__(self, key: str) -> None:
        if not key:
            raise TokenEncryptionError("Encryption key is empty")
        try:
            self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise TokenEncryptionError(f"Invalid Fernet key: {exc}") from exc

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str) or not plaintext:
            raise TokenEncryptionError("Cannot encrypt empty value")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        if not ciphertext:
            raise TokenEncryptionError("Cannot decrypt empty value")
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise TokenEncryptionError("Failed to decrypt token") from exc
