"""Pending-action storage backed by ``users.preferences_json``.

When the assistant needs the user to disambiguate a calendar update or
cancel, it parks the original tool arguments plus the candidate events
on the user row, and waits for the next inbound interactive reply to
resume the action. Pending actions expire after ``PENDING_ACTION_TTL``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import User
from app.services.event_resolver import ResolvedEvent

logger = logging.getLogger(__name__)


PENDING_ACTION_TTL = timedelta(minutes=5)
_PENDING_KEY = "pending_action"


def serialize_event(event: ResolvedEvent) -> dict[str, Any]:
    return asdict(event)


def deserialize_event(payload: dict[str, Any]) -> ResolvedEvent | None:
    try:
        return ResolvedEvent(
            event_id=str(payload["event_id"]),
            title=str(payload["title"]),
            start=str(payload["start"]),
            end=str(payload["end"]),
            calendar_id=str(payload["calendar_id"]),
        )
    except (KeyError, TypeError):
        return None


def set_pending_action(
    user: User,
    *,
    disambiguation_id: str,
    action_type: str,
    arguments: dict[str, Any],
    options: list[ResolvedEvent] | list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> None:
    """Store a pending disambiguation on ``user.preferences_json``.

    ``options`` may be ``ResolvedEvent`` instances (calendar disambiguation)
    or plain ``dict`` payloads (e.g. memory disambiguation). ``extra`` is
    merged into the parked payload for action-type-specific fields like
    ``memory_id``.

    Reassigns the JSONB column so SQLAlchemy notices the change.
    """
    expires_at = datetime.now(timezone.utc) + PENDING_ACTION_TTL
    serialized_options: list[dict[str, Any]] = []
    for opt in options:
        if isinstance(opt, ResolvedEvent):
            serialized_options.append(serialize_event(opt))
        elif isinstance(opt, dict):
            serialized_options.append(dict(opt))
        else:  # pragma: no cover - defensive
            raise TypeError(f"Unsupported pending-action option type: {type(opt)!r}")
    payload: dict[str, Any] = {
        "id": disambiguation_id,
        "type": action_type,
        "arguments": arguments,
        "options": serialized_options,
        "expires_at": expires_at.isoformat(),
    }
    if extra:
        payload.update(extra)
    prefs = dict(user.preferences_json or {})
    prefs[_PENDING_KEY] = payload
    user.preferences_json = prefs


def get_active_pending_action(user: User) -> dict[str, Any] | None:
    """Return the pending action if present and not expired, else ``None``."""
    prefs = user.preferences_json or {}
    pending = prefs.get(_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    if _is_expired(pending):
        return None
    return pending


def clear_pending_action(user: User) -> None:
    prefs = dict(user.preferences_json or {})
    if _PENDING_KEY not in prefs:
        return
    prefs.pop(_PENDING_KEY, None)
    user.preferences_json = prefs


def _is_expired(pending: dict[str, Any]) -> bool:
    raw_expiry = pending.get("expires_at")
    if not isinstance(raw_expiry, str):
        return True
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at
