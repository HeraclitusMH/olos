"""Tests for reminder_query, reminder_update, reminder_cancel tool handlers.

Covers single-match success, multi-match disambiguation, validation
failures, and resume-from-interactive-reply flow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import Reminder
from app.services.message_processor import _process_interactive_reply
from app.services.pending_action import set_pending_action
from app.services.tool_executor import ToolExecutor


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = {}


class _NoOpSession:
    """Stub used by handlers that delegate every DB op to the service."""

    def __init__(self) -> None:
        self.flushes = 0

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
        raise AssertionError("session.execute should not be invoked")

    def add(self, _obj: Any) -> None:  # pragma: no cover
        raise AssertionError("session.add should not be invoked")

    async def delete(self, _obj: Any) -> None:  # pragma: no cover
        raise AssertionError("session.delete should not be invoked")

    async def flush(self) -> None:
        self.flushes += 1


class _FakeWhatsAppClient:
    def __init__(self) -> None:
        self.button_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def send_interactive_buttons(
        self, to: str, body_text: str, buttons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.button_calls.append(
            {"to": to, "body_text": body_text, "buttons": buttons}
        )
        return {"messages": [{"id": "wamid.OUT"}]}

    async def send_interactive_list(
        self,
        to: str,
        body_text: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.list_calls.append(
            {
                "to": to,
                "body_text": body_text,
                "button_text": button_text,
                "sections": sections,
            }
        )
        return {"messages": [{"id": "wamid.OUT"}]}


class _FakeReminderService:
    """Tracks calls and returns canned reminders for handler tests."""

    def __init__(
        self,
        *,
        search_returns: list[Reminder] | None = None,
        get_by_id_returns: Reminder | None = None,
    ) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[Reminder] = []
        self.get_by_id_calls: list[uuid.UUID] = []
        self._search_returns = list(search_returns or [])
        self._get_by_id_returns = get_by_id_returns

    async def search_unsent(
        self,
        user_id: uuid.UUID,
        db: Any,
        *,
        search_text: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        limit: int = 10,
    ) -> list[Reminder]:
        self.search_calls.append(
            {
                "user_id": user_id,
                "search_text": search_text,
                "time_min": time_min,
                "time_max": time_max,
                "limit": limit,
            }
        )
        return list(self._search_returns)

    async def update_reminder(
        self,
        reminder: Reminder,
        *,
        new_text: str | None,
        new_remind_at: datetime | None,
        db: Any,
    ) -> Reminder:
        self.update_calls.append(
            {
                "reminder_id": reminder.id,
                "new_text": new_text,
                "new_remind_at": new_remind_at,
            }
        )
        if new_text is not None:
            reminder.reminder_text = new_text
        if new_remind_at is not None:
            reminder.remind_at = new_remind_at
        return reminder

    async def delete_reminder(self, reminder: Reminder, db: Any) -> None:
        self.delete_calls.append(reminder)

    async def get_by_id(
        self,
        reminder_id: uuid.UUID,
        user_id: uuid.UUID,
        db: Any,
        *,
        include_sent: bool = False,
    ) -> Reminder | None:
        self.get_by_id_calls.append(reminder_id)
        return self._get_by_id_returns


def _make_reminder(
    *,
    user_id: uuid.UUID | None = None,
    text: str = "Stretch",
    remind_at: datetime | None = None,
) -> Reminder:
    reminder = Reminder(
        user_id=user_id or uuid.uuid4(),
        reminder_text=text,
        remind_at=remind_at or (datetime.now(UTC) + timedelta(hours=1)),
    )
    reminder.id = uuid.uuid4()
    reminder.sent = False
    reminder.failed_attempts = 0
    reminder.sent_at = None
    return reminder


# ---------------------------------------------------------------------------
# reminder_query
# ---------------------------------------------------------------------------


async def test_reminder_query_returns_unsent_reminders() -> None:
    user = _DummyUser()
    reminder_a = _make_reminder(
        user_id=user.id,
        text="Invite Kale",
        remind_at=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
    )
    reminder_b = _make_reminder(
        user_id=user.id,
        text="Stretch",
        remind_at=datetime(2026, 5, 15, 7, 0, tzinfo=UTC),
    )
    fake = _FakeReminderService(search_returns=[reminder_a, reminder_b])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_query",
        {},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.data["tool"] == "reminder_query"
    assert result.data["count"] == 2
    assert result.message.startswith("Upcoming reminders:")
    assert "Invite Kale" in result.message
    assert "Stretch" in result.message
    call = fake.search_calls[0]
    assert call["user_id"] == user.id
    assert call["search_text"] is None
    assert call["limit"] == 10


async def test_reminder_query_empty_returns_friendly_message() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_query",
        {"search_text": "ghost"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == "No upcoming reminders found."
    assert fake.search_calls[0]["search_text"] == "ghost"


async def test_reminder_query_rejects_invalid_time_min() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_query",
        {"time_min": "tomorrow"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.data["reason"] == "invalid_datetime"
    assert fake.search_calls == []


# ---------------------------------------------------------------------------
# reminder_update
# ---------------------------------------------------------------------------


async def test_reminder_update_no_changes_asks_clarification() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_update",
        {"search_text": "kale"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "What should I change" in result.message
    assert fake.search_calls == []
    assert fake.update_calls == []


async def test_reminder_update_invalid_new_time_returns_clarification() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_update",
        {"search_text": "kale", "new_remind_at": "tomorrow"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.data["reason"] == "invalid_datetime"


async def test_reminder_update_no_match_returns_not_found() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    new_time = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    result = await executor.execute(
        "reminder_update",
        {"search_text": "ghost", "new_remind_at": new_time},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "No matching reminder" in result.message
    assert "'ghost'" in result.message
    assert fake.update_calls == []


async def test_reminder_update_single_match_applies_change() -> None:
    user = _DummyUser()
    reminder = _make_reminder(
        user_id=user.id,
        text="Invite Kale",
        remind_at=datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
    )
    fake = _FakeReminderService(search_returns=[reminder])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    new_time = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    result = await executor.execute(
        "reminder_update",
        {"search_text": "kale", "new_remind_at": new_time},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "Updated reminder" in result.message
    assert result.data["reminder_id"] == str(reminder.id)
    assert len(fake.update_calls) == 1
    update = fake.update_calls[0]
    assert update["reminder_id"] == reminder.id
    assert update["new_text"] is None
    assert update["new_remind_at"] is not None


async def test_reminder_update_multiple_matches_triggers_disambiguation() -> None:
    user = _DummyUser()
    reminder_a = _make_reminder(
        user_id=user.id, text="Invite Kale", remind_at=datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
    )
    reminder_b = _make_reminder(
        user_id=user.id, text="Invite Kalei", remind_at=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
    )
    fake = _FakeReminderService(search_returns=[reminder_a, reminder_b])
    whatsapp = _FakeWhatsAppClient()
    executor = ToolExecutor(
        reminder_service_factory=lambda: fake,
        whatsapp_client_factory=lambda: whatsapp,
    )

    new_time = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    result = await executor.execute(
        "reminder_update",
        {"search_text": "kale", "new_remind_at": new_time},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == ""
    assert (result.data or {}).get("sent_directly") is True
    assert len(whatsapp.button_calls) == 1
    assert whatsapp.list_calls == []
    assert fake.update_calls == []

    pending = user.preferences_json["pending_action"]
    assert pending["type"] == "reminder_update"
    assert pending["id"] == result.data["disambiguation_id"]
    assert {opt["reminder_id"] for opt in pending["options"]} == {
        str(reminder_a.id),
        str(reminder_b.id),
    }
    assert pending["arguments"]["new_remind_at"] == new_time


# ---------------------------------------------------------------------------
# reminder_cancel
# ---------------------------------------------------------------------------


async def test_reminder_cancel_single_match_deletes_and_confirms() -> None:
    user = _DummyUser()
    reminder = _make_reminder(
        user_id=user.id, text="Invite Kale",
        remind_at=datetime(2026, 5, 15, 9, 0, tzinfo=UTC),
    )
    fake = _FakeReminderService(search_returns=[reminder])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_cancel",
        {"search_text": "kale"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "Cancelled reminder" in result.message
    assert fake.delete_calls == [reminder]
    assert result.data["reminder_id"] == str(reminder.id)


async def test_reminder_cancel_no_match_returns_not_found() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_cancel",
        {"search_text": "ghost"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "No matching reminder" in result.message
    assert fake.delete_calls == []


async def test_reminder_cancel_multiple_matches_triggers_disambiguation() -> None:
    user = _DummyUser()
    reminder_a = _make_reminder(
        user_id=user.id, text="Invite Kale",
        remind_at=datetime(2026, 5, 15, 9, 0, tzinfo=UTC),
    )
    reminder_b = _make_reminder(
        user_id=user.id, text="Invite Kalei",
        remind_at=datetime(2026, 5, 16, 9, 0, tzinfo=UTC),
    )
    fake = _FakeReminderService(search_returns=[reminder_a, reminder_b])
    whatsapp = _FakeWhatsAppClient()
    executor = ToolExecutor(
        reminder_service_factory=lambda: fake,
        whatsapp_client_factory=lambda: whatsapp,
    )

    result = await executor.execute(
        "reminder_cancel",
        {"search_text": "kale"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == ""
    assert fake.delete_calls == []
    assert len(whatsapp.button_calls) == 1
    assert user.preferences_json["pending_action"]["type"] == "reminder_cancel"


async def test_reminder_cancel_with_preselected_skips_search() -> None:
    user = _DummyUser()
    reminder = _make_reminder(user_id=user.id, text="picked")
    fake = _FakeReminderService(search_returns=[])
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_cancel",
        {"search_text": "kale"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
        preselected_reminder=reminder,
    )

    assert result.success is True
    assert fake.search_calls == []
    assert fake.delete_calls == [reminder]


# ---------------------------------------------------------------------------
# Resume pending reminder action (interactive button reply)
# ---------------------------------------------------------------------------


def _park_reminder_pending(
    user: _DummyUser,
    *,
    action_type: str,
    reminders: list[Reminder],
    arguments: dict[str, Any] | None = None,
    disambiguation_id: str = "abc",
) -> None:
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id=disambiguation_id,
        action_type=action_type,
        arguments=arguments or {},
        options=[
            {
                "reminder_id": str(r.id),
                "reminder_text": r.reminder_text,
                "remind_at": r.remind_at.isoformat(),
            }
            for r in reminders
        ],
    )


async def test_pending_reminder_action_resume_update_applies_change() -> None:
    user = _DummyUser()
    target = _make_reminder(
        user_id=user.id, text="Invite Kale",
        remind_at=datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
    )
    other = _make_reminder(
        user_id=user.id, text="Invite Kalei",
        remind_at=datetime(2026, 5, 16, 9, 0, tzinfo=UTC),
    )
    new_time = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    _park_reminder_pending(
        user,
        action_type="reminder_update",
        reminders=[other, target],
        arguments={"search_text": "kale", "new_remind_at": new_time},
    )

    fake = _FakeReminderService(
        get_by_id_returns=target, search_returns=[]
    )
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:1",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
        reminder_service=fake,  # type: ignore[arg-type]
    )

    assert "Updated reminder" in log.combined_reply
    assert "pending_action" not in user.preferences_json
    assert fake.get_by_id_calls == [target.id]
    assert len(fake.update_calls) == 1
    assert fake.update_calls[0]["reminder_id"] == target.id
    assert fake.update_calls[0]["new_remind_at"] is not None


async def test_pending_reminder_action_resume_cancel_deletes_selected() -> None:
    user = _DummyUser()
    target = _make_reminder(user_id=user.id, text="Invite Kale")
    other = _make_reminder(user_id=user.id, text="Invite Kalei")
    _park_reminder_pending(
        user,
        action_type="reminder_cancel",
        reminders=[other, target],
        arguments={"search_text": "kale"},
    )

    fake = _FakeReminderService(get_by_id_returns=target)
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:1",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
        reminder_service=fake,  # type: ignore[arg-type]
    )

    assert "Cancelled reminder" in log.combined_reply
    assert fake.delete_calls == [target]
    assert "pending_action" not in user.preferences_json


async def test_pending_reminder_action_returns_unavailable_when_gone() -> None:
    user = _DummyUser()
    target = _make_reminder(user_id=user.id, text="Invite Kale")
    _park_reminder_pending(
        user,
        action_type="reminder_cancel",
        reminders=[target],
        arguments={"search_text": "kale"},
    )

    fake = _FakeReminderService(get_by_id_returns=None)
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:0",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
        reminder_service=fake,  # type: ignore[arg-type]
    )

    assert "no longer available" in log.combined_reply
    assert fake.delete_calls == []
    assert "pending_action" not in user.preferences_json
