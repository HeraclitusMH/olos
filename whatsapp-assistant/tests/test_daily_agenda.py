"""Tests for the daily-agenda scheduler service."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.models import GoogleAccount, User
from app.services.daily_agenda import (
    AgendaPrefs,
    DailyAgendaService,
    _resolve_zone,
    format_agenda_message,
    get_user_prefs,
    is_due,
    set_user_prefs,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _make_user(
    *,
    wa_id: str = "34600111222",
    prefs: dict[str, Any] | None = None,
) -> User:
    user = User(
        wa_id=wa_id,
        timezone="Europe/Madrid",
        locale="en",
        preferences_json=prefs or {},
    )
    user.id = uuid.uuid4()
    return user


def _make_account(user_id: uuid.UUID) -> GoogleAccount:
    account = GoogleAccount(
        user_id=user_id,
        access_token_enc=b"x",
        refresh_token_enc=b"y",
        token_expires_at=None,  # type: ignore[arg-type]
        calendar_id="primary",
        status="active",
    )
    account.id = uuid.uuid4()
    return account


class _UsersResult:
    def __init__(self, users: list[User]) -> None:
        self._users = users

    def scalars(self) -> "_UsersResult":
        return self

    def all(self) -> list[User]:
        return self._users

    def first(self):  # pragma: no cover - unused in users path
        return self._users[0] if self._users else None


class _AccountResult:
    def __init__(self, account: GoogleAccount | None) -> None:
        self._account = account

    def scalars(self) -> "_AccountResult":
        return self

    def first(self) -> GoogleAccount | None:
        return self._account


class _ScalarResult:
    """Mimics ``execute(...).scalar_one_or_none()`` / ``.scalar()``."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value


class _FakeSession:
    """Routes execute() by stmt type. Tracks add()/commit() and exit calls."""

    def __init__(
        self,
        *,
        users: list[User] | None = None,
        account: GoogleAccount | None = None,
        already_sent: bool = False,
    ) -> None:
        self.users = users or []
        self.account = account
        self.already_sent = already_sent
        self.added: list[Any] = []
        self.commits = 0
        self.rolled_back = 0
        self.advisory_lock_calls = 0
        self.advisory_unlock_calls = 0

    async def execute(self, stmt: Any) -> Any:
        # Raw text() statements for advisory locks.
        text_value = getattr(stmt, "text", None)
        if isinstance(text_value, str):
            if "pg_try_advisory_lock" in text_value:
                self.advisory_lock_calls += 1
                return _ScalarResult(True)
            if "pg_advisory_unlock" in text_value:
                self.advisory_unlock_calls += 1
                return _ScalarResult(True)

        # SQLAlchemy select() — inspect the column being selected.
        column_descriptions = getattr(stmt, "column_descriptions", None)
        if column_descriptions:
            entity = column_descriptions[0].get("entity")
            if entity is User:
                return _UsersResult(self.users)
            if entity is GoogleAccount:
                return _AccountResult(self.account)
        # Fallback: assume it's a "did we already send" probe.
        return _ScalarResult(uuid.uuid4() if self.already_sent else None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _session_factory(*sessions: _FakeSession):
    """Return a callable that yields the given sessions in order."""
    queue = list(sessions)

    def factory() -> _FakeSession:
        if len(queue) == 1:
            return queue[0]
        return queue.pop(0)

    return factory


class _FakeAuth:
    def __init__(self, token: str | None = "ya29.fresh") -> None:
        self._token = token

    async def get_valid_token(self, _user_id: uuid.UUID) -> str | None:
        return self._token


class _FakeCalendar:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self._events = events or []
        self.calls: list[dict[str, Any]] = []

    async def list_events(
        self, time_min: str, time_max: str, query: str | None = None, max_results: int = 20
    ) -> list[dict[str, Any]]:
        self.calls.append({"time_min": time_min, "time_max": time_max})
        return self._events


class _FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.template_calls: list[dict[str, str]] = []

    async def send_text_message(self, to: str, text: str) -> dict[str, Any]:
        self.sent.append((to, text))
        return {"ok": True}

    async def send_template_message(
        self,
        to: str,
        template_name: str,
        body_text: str,
        language_code: str = "en",
    ) -> dict[str, Any]:
        self.sent.append((to, body_text))
        self.template_calls.append(
            {"to": to, "template_name": template_name, "language_code": language_code}
        )
        return {"ok": True}


# ---------------------------------------------------------------------------
# Preference resolution
# ---------------------------------------------------------------------------


def test_default_prefs_when_user_has_none() -> None:
    user = _make_user()
    prefs = get_user_prefs(user)
    assert prefs.enabled is True
    assert prefs.timezone == "Asia/Makassar"
    assert prefs.time_local == "08:00"


def test_user_prefs_override_defaults() -> None:
    user = _make_user(
        prefs={
            "daily_agenda": {
                "enabled": False,
                "timezone": "Europe/Madrid",
                "time_local": "07:30",
            }
        }
    )
    prefs = get_user_prefs(user)
    assert prefs.enabled is False
    assert prefs.timezone == "Europe/Madrid"
    assert prefs.time_local == "07:30"


def test_set_user_prefs_reassigns_jsonb() -> None:
    user = _make_user(prefs={"other_key": {"keep": True}})
    set_user_prefs(
        user,
        AgendaPrefs(enabled=True, timezone="Europe/Madrid", time_local="09:00"),
    )
    # Preserves unrelated keys.
    assert user.preferences_json["other_key"] == {"keep": True}
    assert user.preferences_json["daily_agenda"]["timezone"] == "Europe/Madrid"
    assert user.preferences_json["daily_agenda"]["time_local"] == "09:00"


def test_invalid_timezone_falls_back_to_default() -> None:
    zone, name = _resolve_zone("Not/AZone")
    assert name == "Asia/Makassar"
    assert isinstance(zone, ZoneInfo)


# ---------------------------------------------------------------------------
# Due-window logic
# ---------------------------------------------------------------------------


def _now_utc_for_local(local_dt: datetime) -> datetime:
    return local_dt.astimezone(UTC)


def test_due_at_two_minutes_past_scheduled_time_makassar() -> None:
    prefs = AgendaPrefs(enabled=True, timezone="Asia/Makassar", time_local="08:00")
    local = datetime(2026, 5, 11, 8, 2, tzinfo=ZoneInfo("Asia/Makassar"))
    due, local_date = is_due(prefs, _now_utc_for_local(local))
    assert due is True
    assert local_date == date(2026, 5, 11)


def test_not_due_before_scheduled_time_makassar() -> None:
    prefs = AgendaPrefs(enabled=True, timezone="Asia/Makassar", time_local="08:00")
    local = datetime(2026, 5, 11, 7, 50, tzinfo=ZoneInfo("Asia/Makassar"))
    due, _ = is_due(prefs, _now_utc_for_local(local))
    assert due is False


def test_not_due_after_window_closes_makassar() -> None:
    prefs = AgendaPrefs(enabled=True, timezone="Asia/Makassar", time_local="08:00")
    local = datetime(2026, 5, 11, 8, 10, tzinfo=ZoneInfo("Asia/Makassar"))
    due, _ = is_due(prefs, _now_utc_for_local(local), window_minutes=5)
    assert due is False


def test_due_window_works_for_madrid_timezone() -> None:
    prefs = AgendaPrefs(enabled=True, timezone="Europe/Madrid", time_local="09:30")
    # 09:31 Madrid local — inside window.
    local = datetime(2026, 5, 11, 9, 31, tzinfo=ZoneInfo("Europe/Madrid"))
    due, local_date = is_due(prefs, _now_utc_for_local(local))
    assert due is True
    assert local_date == date(2026, 5, 11)


def test_due_uses_local_date_not_utc_date() -> None:
    """At 00:30 Asia/Makassar on 11 May it's still 10 May in UTC."""
    prefs = AgendaPrefs(enabled=True, timezone="Asia/Makassar", time_local="00:15")
    local = datetime(2026, 5, 11, 0, 17, tzinfo=ZoneInfo("Asia/Makassar"))
    due, local_date = is_due(prefs, _now_utc_for_local(local))
    assert due is True
    assert local_date == date(2026, 5, 11)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def test_format_no_events_message() -> None:
    msg = format_agenda_message([], date(2026, 5, 11), "Asia/Makassar")
    assert msg == "Good morning! You have no events today."


def test_format_timed_events() -> None:
    events = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-05-11T09:30:00+08:00"},
            "end": {"dateTime": "2026-05-11T10:15:00+08:00"},
        }
    ]
    msg = format_agenda_message(events, date(2026, 5, 11), "Asia/Makassar")
    assert "Good morning! Here are your events for Mon, 11 May (your time):" in msg
    assert "• 09:30–10:15 — Standup" in msg


def test_format_all_day_event() -> None:
    events = [
        {
            "summary": "Holiday",
            "start": {"date": "2026-05-11"},
            "end": {"date": "2026-05-12"},
        }
    ]
    msg = format_agenda_message(events, date(2026, 5, 11), "Asia/Makassar")
    assert "• (All day) — Holiday" in msg


# ---------------------------------------------------------------------------
# Idempotency / DailyAgendaService.run_tick
# ---------------------------------------------------------------------------


def _build_service(
    sessions: list[_FakeSession],
    calendar: _FakeCalendar,
    whatsapp: _FakeWhatsApp,
    token: str | None = "ya29.fresh",
) -> DailyAgendaService:
    auth = _FakeAuth(token=token)
    queue = list(sessions)

    def session_factory():
        if len(queue) == 1:
            return queue[0]
        return queue.pop(0)

    return DailyAgendaService(
        session_factory=session_factory,
        whatsapp_client_factory=lambda: whatsapp,
        google_auth_factory=lambda _session: auth,
        calendar_service_factory=lambda _token, _cal_id: calendar,
    )


def _due_now_utc() -> datetime:
    local = datetime(2026, 5, 11, 8, 2, tzinfo=ZoneInfo("Asia/Makassar"))
    return local.astimezone(UTC)


@pytest.mark.asyncio
async def test_run_tick_sends_and_records_for_due_user() -> None:
    user = _make_user()
    account = _make_account(user.id)
    load_session = _FakeSession(users=[user])
    process_session = _FakeSession(account=account, already_sent=False)

    calendar = _FakeCalendar(
        events=[
            {
                "summary": "Standup",
                "start": {"dateTime": "2026-05-11T09:30:00+08:00"},
                "end": {"dateTime": "2026-05-11T10:15:00+08:00"},
            }
        ]
    )
    whatsapp = _FakeWhatsApp()
    service = _build_service([load_session, process_session], calendar, whatsapp)

    sent = await service.run_tick(now_utc=_due_now_utc())

    assert sent == 1
    assert len(whatsapp.sent) == 1
    to, text = whatsapp.sent[0]
    assert to == user.wa_id
    assert "Standup" in text
    # A DailyAgendaSend was added and committed.
    from app.models import DailyAgendaSend

    sends = [obj for obj in process_session.added if isinstance(obj, DailyAgendaSend)]
    assert len(sends) == 1
    assert sends[0].user_id == user.id
    assert sends[0].agenda_date == date(2026, 5, 11)
    assert sends[0].timezone == "Asia/Makassar"
    assert process_session.commits == 1
    # Advisory lock was acquired and released.
    assert load_session.advisory_lock_calls == 1
    assert load_session.advisory_unlock_calls == 1


@pytest.mark.asyncio
async def test_run_tick_is_idempotent_if_already_sent_today() -> None:
    user = _make_user()
    account = _make_account(user.id)
    load_session = _FakeSession(users=[user])
    process_session = _FakeSession(account=account, already_sent=True)

    calendar = _FakeCalendar(events=[])
    whatsapp = _FakeWhatsApp()
    service = _build_service([load_session, process_session], calendar, whatsapp)

    sent = await service.run_tick(now_utc=_due_now_utc())

    assert sent == 0
    assert whatsapp.sent == []
    from app.models import DailyAgendaSend

    assert not any(
        isinstance(obj, DailyAgendaSend) for obj in process_session.added
    )


@pytest.mark.asyncio
async def test_run_tick_skips_disabled_users() -> None:
    user = _make_user(
        prefs={
            "daily_agenda": {
                "enabled": False,
                "timezone": "Asia/Makassar",
                "time_local": "08:00",
            }
        }
    )
    load_session = _FakeSession(users=[user])
    calendar = _FakeCalendar(events=[])
    whatsapp = _FakeWhatsApp()
    # Only one session is needed because the user is filtered out before
    # the per-user processing session is opened.
    service = _build_service([load_session], calendar, whatsapp)

    sent = await service.run_tick(now_utc=_due_now_utc())

    assert sent == 0
    assert whatsapp.sent == []


@pytest.mark.asyncio
async def test_run_tick_skips_user_not_in_window() -> None:
    user = _make_user()
    load_session = _FakeSession(users=[user])
    calendar = _FakeCalendar(events=[])
    whatsapp = _FakeWhatsApp()
    service = _build_service([load_session], calendar, whatsapp)

    # 06:00 Makassar — well before the 08:00 window.
    too_early_local = datetime(
        2026, 5, 11, 6, 0, tzinfo=ZoneInfo("Asia/Makassar")
    )
    sent = await service.run_tick(now_utc=too_early_local.astimezone(UTC))

    assert sent == 0
    assert whatsapp.sent == []


@pytest.mark.asyncio
async def test_run_tick_sends_no_events_message_when_calendar_empty() -> None:
    user = _make_user()
    account = _make_account(user.id)
    load_session = _FakeSession(users=[user])
    process_session = _FakeSession(account=account, already_sent=False)

    calendar = _FakeCalendar(events=[])
    whatsapp = _FakeWhatsApp()
    service = _build_service([load_session, process_session], calendar, whatsapp)

    sent = await service.run_tick(now_utc=_due_now_utc())

    assert sent == 1
    assert len(whatsapp.sent) == 1
    _, text = whatsapp.sent[0]
    assert text == "Good morning! You have no events today."


@pytest.mark.asyncio
async def test_run_tick_skips_user_with_no_google_account() -> None:
    user = _make_user()
    load_session = _FakeSession(users=[user])
    # account=None → service skips the user.
    process_session = _FakeSession(account=None, already_sent=False)

    calendar = _FakeCalendar(events=[])
    whatsapp = _FakeWhatsApp()
    service = _build_service(
        [load_session, process_session],
        calendar,
        whatsapp,
        token=None,  # No Google account = no token.
    )

    sent = await service.run_tick(now_utc=_due_now_utc())

    assert sent == 0
    assert whatsapp.sent == []


@pytest.mark.asyncio
async def test_run_tick_uses_template_message_with_configured_name() -> None:
    """Agenda sends must use send_template_message, not send_text_message."""
    import os

    from app.config import get_settings

    user = _make_user()
    account = _make_account(user.id)
    load_session = _FakeSession(users=[user])
    process_session = _FakeSession(account=account, already_sent=False)

    calendar = _FakeCalendar(events=[])
    whatsapp = _FakeWhatsApp()
    service = _build_service([load_session, process_session], calendar, whatsapp)

    os.environ["WHATSAPP_TEMPLATE_NAME"] = "my_agenda_template"
    os.environ["WHATSAPP_TEMPLATE_LANGUAGE"] = "en_US"
    get_settings.cache_clear()

    try:
        sent = await service.run_tick(now_utc=_due_now_utc())
    finally:
        del os.environ["WHATSAPP_TEMPLATE_NAME"]
        del os.environ["WHATSAPP_TEMPLATE_LANGUAGE"]
        get_settings.cache_clear()

    assert sent == 1
    assert len(whatsapp.template_calls) == 1
    call = whatsapp.template_calls[0]
    assert call["to"] == user.wa_id
    assert call["template_name"] == "my_agenda_template"
    assert call["language_code"] == "en_US"
