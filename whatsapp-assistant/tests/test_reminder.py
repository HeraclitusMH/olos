"""Tests for the reminder service, tool handler and scheduler tick."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models import Reminder, User
from app.services.reminder import (
    ReminderError,
    ReminderScheduler,
    ReminderService,
)
from app.services.tool_executor import ToolExecutor
from app.services.whatsapp import WhatsAppAPIError


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = {}


class _CapturingSession:
    """Fake AsyncSession that records add()/flush()/commit() calls."""

    def __init__(self, *, due_reminders: list[Reminder] | None = None) -> None:
        self.added: list[Any] = []
        self.flushes = 0
        self.commits = 0
        self.rolled_back = 0
        self._due_reminders = due_reminders or []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def execute(self, _stmt: Any) -> Any:
        class _Result:
            def __init__(self, items: list[Reminder]) -> None:
                self._items = items

            def scalars(self) -> "_Result":
                return self

            def all(self) -> list[Reminder]:
                return list(self._items)

        return _Result(self._due_reminders)

    async def __aenter__(self) -> "_CapturingSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeWhatsApp:
    def __init__(
        self,
        *,
        text_raises: Exception | None = None,
        template_raises: Exception | None = None,
    ) -> None:
        self.text_calls: list[dict[str, str]] = []
        self.template_calls: list[dict[str, str]] = []
        self._text_raises = text_raises
        self._template_raises = template_raises

    async def send_text_message(self, to: str, text: str) -> dict[str, Any]:
        self.text_calls.append({"to": to, "text": text})
        if self._text_raises is not None:
            raise self._text_raises
        return {"ok": True}

    async def send_template_message(
        self,
        to: str,
        template_name: str,
        body_text: str,
        language_code: str = "en",
    ) -> dict[str, Any]:
        self.template_calls.append(
            {
                "to": to,
                "template_name": template_name,
                "body_text": body_text,
                "language_code": language_code,
            }
        )
        if self._template_raises is not None:
            raise self._template_raises
        return {"ok": True}


def _make_reminder(
    *,
    user: User | _DummyUser | None = None,
    remind_at: datetime | None = None,
    text: str = "Stretch",
    sent: bool = False,
    failed_attempts: int = 0,
) -> Reminder:
    reminder = Reminder(
        user_id=user.id if user else uuid.uuid4(),
        reminder_text=text,
        remind_at=remind_at or (datetime.now(UTC) - timedelta(seconds=5)),
    )
    reminder.id = uuid.uuid4()
    reminder.sent = sent
    reminder.failed_attempts = failed_attempts
    reminder.sent_at = None
    reminder.user = user  # type: ignore[assignment]
    return reminder


# ---------------------------------------------------------------------------
# ReminderService
# ---------------------------------------------------------------------------


async def test_create_reminder_happy_path() -> None:
    service = ReminderService()
    session = _CapturingSession()
    user_id = uuid.uuid4()
    remind_at = datetime.now(UTC) + timedelta(minutes=2)

    reminder = await service.create_reminder(
        user_id=user_id,
        reminder_text="  call dentist  ",
        remind_at=remind_at,
        db=session,  # type: ignore[arg-type]
    )

    assert reminder.user_id == user_id
    assert reminder.reminder_text == "call dentist"
    assert reminder.remind_at == remind_at
    assert session.added == [reminder]
    assert session.flushes == 1


async def test_create_reminder_rejects_past_time() -> None:
    service = ReminderService()
    session = _CapturingSession()
    past = datetime.now(UTC) - timedelta(minutes=5)

    with pytest.raises(ReminderError):
        await service.create_reminder(
            user_id=uuid.uuid4(),
            reminder_text="too late",
            remind_at=past,
            db=session,  # type: ignore[arg-type]
        )

    assert session.added == []


async def test_create_reminder_rejects_naive_datetime() -> None:
    service = ReminderService()
    session = _CapturingSession()
    naive = datetime.now() + timedelta(minutes=5)  # noqa: DTZ005

    with pytest.raises(ReminderError):
        await service.create_reminder(
            user_id=uuid.uuid4(),
            reminder_text="naive",
            remind_at=naive,
            db=session,  # type: ignore[arg-type]
        )


async def test_create_reminder_rejects_too_far_in_future() -> None:
    service = ReminderService()
    session = _CapturingSession()
    far = datetime.now(UTC) + timedelta(days=400)

    with pytest.raises(ReminderError):
        await service.create_reminder(
            user_id=uuid.uuid4(),
            reminder_text="too far",
            remind_at=far,
            db=session,  # type: ignore[arg-type]
        )


async def test_get_due_reminders_uses_select_and_orders() -> None:
    user = _DummyUser()
    reminder_a = _make_reminder(
        user=user,
        remind_at=datetime.now(UTC) - timedelta(seconds=30),
        text="A",
    )
    reminder_b = _make_reminder(
        user=user,
        remind_at=datetime.now(UTC) - timedelta(seconds=5),
        text="B",
    )
    session = _CapturingSession(due_reminders=[reminder_a, reminder_b])

    service = ReminderService()
    due = await service.get_due_reminders(datetime.now(UTC), session)  # type: ignore[arg-type]

    assert due == [reminder_a, reminder_b]


async def test_mark_sent_updates_flags() -> None:
    user = _DummyUser()
    reminder = _make_reminder(user=user)
    session = _CapturingSession()

    service = ReminderService()
    await service.mark_sent(reminder, session)  # type: ignore[arg-type]

    assert reminder.sent is True
    assert reminder.sent_at is not None
    assert session.flushes == 1


async def test_increment_failed_attempts_increments_and_flushes() -> None:
    user = _DummyUser()
    reminder = _make_reminder(user=user, failed_attempts=1)
    session = _CapturingSession()

    service = ReminderService()
    count = await service.increment_failed_attempts(reminder, session)  # type: ignore[arg-type]

    assert count == 2
    assert reminder.failed_attempts == 2
    assert session.flushes == 1


# ---------------------------------------------------------------------------
# Tool executor handler
# ---------------------------------------------------------------------------


class _FakeReminderService:
    def __init__(self, *, raises: ReminderError | None = None) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self._raises = raises

    async def create_reminder(
        self,
        *,
        user_id: uuid.UUID,
        reminder_text: str,
        remind_at: datetime,
        db: Any,
    ) -> Reminder:
        self.create_calls.append(
            {
                "user_id": user_id,
                "reminder_text": reminder_text,
                "remind_at": remind_at,
            }
        )
        if self._raises is not None:
            raise self._raises
        reminder = Reminder(
            user_id=user_id,
            reminder_text=reminder_text,
            remind_at=remind_at,
        )
        reminder.id = uuid.uuid4()
        return reminder


async def test_handle_reminder_create_happy_path() -> None:
    user = _DummyUser()
    fake = _FakeReminderService()
    executor = ToolExecutor(reminder_service_factory=lambda: fake)
    remind_at = datetime.now(UTC) + timedelta(minutes=2)

    result = await executor.execute(
        "reminder_create",
        {"reminder_text": "stretch", "remind_at": remind_at.isoformat()},
        user=user,  # type: ignore[arg-type]
        db=_CapturingSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message.startswith("Reminder set for")
    assert result.data["tool"] == "reminder_create"
    assert "reminder_id" in result.data
    call = fake.create_calls[0]
    assert call["reminder_text"] == "stretch"
    assert call["user_id"] == user.id


async def test_handle_reminder_create_rejects_missing_args() -> None:
    user = _DummyUser()
    fake = _FakeReminderService()
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_create",
        {"reminder_text": "", "remind_at": ""},
        user=user,  # type: ignore[arg-type]
        db=_CapturingSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "remind you about" in result.message
    assert fake.create_calls == []


async def test_handle_reminder_create_rejects_invalid_datetime() -> None:
    user = _DummyUser()
    fake = _FakeReminderService()
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_create",
        {"reminder_text": "stretch", "remind_at": "tomorrow"},
        user=user,  # type: ignore[arg-type]
        db=_CapturingSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.data["reason"] == "invalid_datetime"
    assert fake.create_calls == []


async def test_handle_reminder_create_surfaces_service_error() -> None:
    user = _DummyUser()
    fake = _FakeReminderService(
        raises=ReminderError(
            log_message="past", user_message="That time is in the past."
        )
    )
    executor = ToolExecutor(reminder_service_factory=lambda: fake)

    result = await executor.execute(
        "reminder_create",
        {
            "reminder_text": "stretch",
            "remind_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        },
        user=user,  # type: ignore[arg-type]
        db=_CapturingSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.message == "That time is in the past."


# ---------------------------------------------------------------------------
# Scheduler tick
# ---------------------------------------------------------------------------


def _session_factory(*sessions: _CapturingSession):
    queue = list(sessions)

    def factory() -> _CapturingSession:
        if len(queue) == 1:
            return queue[0]
        return queue.pop(0)

    return factory


async def test_scheduler_tick_sends_text_and_marks_sent() -> None:
    user = _DummyUser()
    reminder = _make_reminder(user=user, text="drink water")
    session = _CapturingSession(due_reminders=[reminder])
    whatsapp = _FakeWhatsApp()

    scheduler = ReminderScheduler(
        session_factory=_session_factory(session),
        whatsapp_client_factory=lambda: whatsapp,
    )

    sent = await scheduler.run_tick()

    assert sent == 1
    assert whatsapp.text_calls == [
        {"to": user.wa_id, "text": "Reminder: drink water"}
    ]
    assert whatsapp.template_calls == []
    assert reminder.sent is True
    assert reminder.sent_at is not None
    assert session.commits == 1


async def test_scheduler_tick_falls_back_to_template_on_24h_window() -> None:
    user = _DummyUser()
    reminder = _make_reminder(user=user, text="call mom")
    session = _CapturingSession(due_reminders=[reminder])
    whatsapp = _FakeWhatsApp(
        text_raises=WhatsAppAPIError(
            status_code=400,
            body='{"error":{"code":131026,"message":"outside 24h"}}',
        )
    )

    scheduler = ReminderScheduler(
        session_factory=_session_factory(session),
        whatsapp_client_factory=lambda: whatsapp,
    )

    sent = await scheduler.run_tick()

    assert sent == 1
    assert len(whatsapp.text_calls) == 1
    assert len(whatsapp.template_calls) == 1
    call = whatsapp.template_calls[0]
    assert call["to"] == user.wa_id
    assert call["body_text"] == "Reminder: call mom"
    assert call["template_name"] == "reminder"
    assert reminder.sent is True


async def test_scheduler_tick_other_whatsapp_errors_increment_attempts() -> None:
    user = _DummyUser()
    reminder = _make_reminder(user=user, text="x", failed_attempts=0)
    session = _CapturingSession(due_reminders=[reminder])
    # 500 — non-24h error. Counts as a normal failure.
    whatsapp = _FakeWhatsApp(
        text_raises=WhatsAppAPIError(status_code=500, body="boom"),
    )

    scheduler = ReminderScheduler(
        session_factory=_session_factory(session),
        whatsapp_client_factory=lambda: whatsapp,
    )

    sent = await scheduler.run_tick()

    assert sent == 0
    assert reminder.failed_attempts == 1
    assert reminder.sent is False


async def test_scheduler_tick_gives_up_after_max_attempts() -> None:
    user = _DummyUser()
    # Already at max - 1 attempts. This tick pushes it to max.
    reminder = _make_reminder(user=user, text="x", failed_attempts=2)
    session = _CapturingSession(due_reminders=[reminder])
    whatsapp = _FakeWhatsApp(
        text_raises=WhatsAppAPIError(status_code=500, body="still broken"),
    )

    scheduler = ReminderScheduler(
        session_factory=_session_factory(session),
        whatsapp_client_factory=lambda: whatsapp,
    )

    sent = await scheduler.run_tick()

    assert sent == 0
    assert reminder.failed_attempts == 3
    # Marked sent so it does not retry forever.
    assert reminder.sent is True


async def test_scheduler_tick_isolates_one_failing_reminder() -> None:
    user = _DummyUser()
    good = _make_reminder(user=user, text="good")
    bad = _make_reminder(user=user, text="bad")
    # Bad reminder will raise during send_text_message AND template_message.
    session = _CapturingSession(due_reminders=[bad, good])

    class _SelectiveWhatsApp(_FakeWhatsApp):
        async def send_text_message(self, to: str, text: str) -> dict[str, Any]:
            self.text_calls.append({"to": to, "text": text})
            if "bad" in text:
                raise WhatsAppAPIError(status_code=500, body="bad")
            return {"ok": True}

    whatsapp = _SelectiveWhatsApp()
    scheduler = ReminderScheduler(
        session_factory=_session_factory(session),
        whatsapp_client_factory=lambda: whatsapp,
    )

    sent = await scheduler.run_tick()

    assert sent == 1
    assert good.sent is True
    assert bad.sent is False
    assert bad.failed_attempts == 1
