"""Generate a fresh Fernet encryption key.

Run with ``python -m app.utils.generate_key`` and copy the printed value into
``.env`` as ``ENCRYPTION_KEY``. The output is a urlsafe-base64-encoded 32-byte
key consumable by :class:`app.utils.encryption.TokenEncryption`.
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def generate_key() -> str:
    return Fernet.generate_key().decode("utf-8")


if __name__ == "__main__":
    print(generate_key())
