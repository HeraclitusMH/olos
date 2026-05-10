"""Encode/decode the OAuth ``state`` parameter with CSRF and TTL protection.

The state parameter is the only mechanism to bind the Google OAuth callback
back to the WhatsApp user who initiated the flow. We encrypt the wa_id with
Fernet so the state is both tamper-resistant (HMAC) and time-limited (TTL),
preventing replay and CSRF attacks.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken


class StateError(Exception):
    """Raised when the OAuth state parameter is invalid, tampered, or expired."""


class OAuthStateCodec:
    DEFAULT_TTL_SECONDS = 600

    def __init__(self, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        try:
            self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise StateError(f"Invalid signing key: {exc}") from exc
        self._ttl = ttl_seconds

    def encode(self, wa_id: str) -> str:
        if not wa_id:
            raise StateError("wa_id is required")
        payload = json.dumps({"wa_id": wa_id}, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload).decode("utf-8")

    def decode(self, state: str) -> str:
        if not state:
            raise StateError("Empty state")
        try:
            payload = self._fernet.decrypt(state.encode("utf-8"), ttl=self._ttl)
        except InvalidToken as exc:
            raise StateError("Invalid or expired state") from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StateError("Malformed state payload") from exc
        wa_id = data.get("wa_id")
        if not wa_id or not isinstance(wa_id, str):
            raise StateError("Malformed state payload")
        return wa_id
