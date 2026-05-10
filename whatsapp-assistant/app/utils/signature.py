"""Webhook signature validation for Meta/WhatsApp webhooks."""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def validate_webhook_signature(
    payload: bytes, signature_header: str | None, app_secret: str
) -> bool:
    """Validate Meta webhook signature using HMAC-SHA256.

    Meta sends the signature in the ``X-Hub-Signature-256`` header in the
    format ``sha256=<hex_digest>`` where the digest is the HMAC-SHA256 of the
    raw request body, keyed with the app secret.
    """
    if not signature_header:
        logger.warning("Missing X-Hub-Signature-256 header")
        return False

    if not signature_header.startswith("sha256="):
        logger.warning("Malformed signature header: missing sha256= prefix")
        return False

    received_digest = signature_header.removeprefix("sha256=")
    expected_digest = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(received_digest, expected_digest)
