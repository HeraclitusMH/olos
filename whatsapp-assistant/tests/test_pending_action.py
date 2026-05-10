from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.event_resolver import ResolvedEvent
from app.services.pending_action import (
    PENDING_ACTION_TTL,
    clear_pending_action,
    deserialize_event,
    get_active_pending_action,
    set_pending_action,
)


class _FakeUser:
    def __init__(self) -> None:
        self.preferences_json: dict[str, Any] = {}


def _event(event_id: str = "evt-1") -> ResolvedEvent:
    return ResolvedEvent(
        event_id=event_id,
        title="Guitar",
        start="2026-05-13T16:00:00+02:00",
        end="2026-05-13T17:00:00+02:00",
        calendar_id="primary",
    )


def test_set_pending_action_stores_payload_and_options() -> None:
    user = _FakeUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_update",
        arguments={"new_start": "2026-05-13T17:00:00+02:00"},
        options=[_event("evt-1"), _event("evt-2")],
    )

    pending = user.preferences_json["pending_action"]
    assert pending["id"] == "abc"
    assert pending["type"] == "calendar_update"
    assert pending["arguments"] == {"new_start": "2026-05-13T17:00:00+02:00"}
    assert len(pending["options"]) == 2
    assert pending["options"][0]["event_id"] == "evt-1"
    assert "expires_at" in pending


def test_get_active_pending_action_returns_none_for_empty_user() -> None:
    user = _FakeUser()
    assert get_active_pending_action(user) is None  # type: ignore[arg-type]


def test_get_active_pending_action_returns_payload_when_fresh() -> None:
    user = _FakeUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_cancel",
        arguments={},
        options=[_event()],
    )

    pending = get_active_pending_action(user)  # type: ignore[arg-type]

    assert pending is not None
    assert pending["id"] == "abc"


def test_get_active_pending_action_returns_none_when_expired() -> None:
    user = _FakeUser()
    user.preferences_json = {
        "pending_action": {
            "id": "abc",
            "type": "calendar_update",
            "arguments": {},
            "options": [],
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        }
    }

    assert get_active_pending_action(user) is None  # type: ignore[arg-type]


def test_clear_pending_action_removes_key() -> None:
    user = _FakeUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_update",
        arguments={},
        options=[_event()],
    )

    clear_pending_action(user)  # type: ignore[arg-type]

    assert "pending_action" not in user.preferences_json


def test_pending_action_ttl_is_five_minutes() -> None:
    assert PENDING_ACTION_TTL == timedelta(minutes=5)


def test_deserialize_event_round_trips() -> None:
    user = _FakeUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_cancel",
        arguments={},
        options=[_event("evt-9")],
    )
    payload = user.preferences_json["pending_action"]["options"][0]

    event = deserialize_event(payload)

    assert event is not None
    assert event.event_id == "evt-9"
    assert event.calendar_id == "primary"
