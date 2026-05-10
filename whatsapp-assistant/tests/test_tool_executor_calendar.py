from __future__ import annotations

import uuid
from typing import Any

from app.models import GoogleAccount
from app.services.google_calendar import (
    GoogleCalendarRateLimited,
    GoogleCalendarUnauthorized,
)
from app.services.tool_executor import ToolExecutor


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"


class _AccountResult:
    def __init__(self, account: GoogleAccount | None) -> None:
        self._account = account

    def scalars(self) -> "_AccountResult":
        return self

    def first(self) -> GoogleAccount | None:
        return self._account


class _FakeSession:
    """Captures session.add() calls and returns the configured account on execute."""

    def __init__(self, account: GoogleAccount | None) -> None:
        self._account = account
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt: Any) -> _AccountResult:
        return _AccountResult(self._account)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


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


class _FakeAuth:
    def __init__(self, token: str | None, refresh_token: str | None = None) -> None:
        self._token = token
        self._refresh_token = refresh_token
        self.refresh_calls = 0

    async def get_valid_token(self, _user_id: uuid.UUID) -> str | None:
        return self._token

    async def refresh_token(self, _account: GoogleAccount) -> str:
        self.refresh_calls += 1
        if self._refresh_token is None:
            raise RuntimeError("refresh failed")
        return self._refresh_token


class _FakeCalendar:
    """Programmable fake replacing GoogleCalendarService."""

    def __init__(
        self,
        *,
        create_returns: Any = None,
        list_returns: Any = None,
        create_raises: list[Exception] | None = None,
        list_raises: list[Exception] | None = None,
    ) -> None:
        self._create_returns = create_returns
        self._list_returns = list_returns
        self._create_raises = list(create_raises or [])
        self._list_raises = list(list_raises or [])
        self.create_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def create_event(
        self,
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        timezone: str = "Europe/Madrid",
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "title": title,
                "start": start,
                "end": end,
                "description": description,
                "timezone": timezone,
            }
        )
        if self._create_raises:
            raise self._create_raises.pop(0)
        return self._create_returns or {}

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        query: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        self.list_calls.append(
            {
                "time_min": time_min,
                "time_max": time_max,
                "query": query,
            }
        )
        if self._list_raises:
            raise self._list_raises.pop(0)
        return self._list_returns or []


def _build_executor(
    *,
    token: str | None,
    calendar: _FakeCalendar,
    refresh_token: str | None = None,
) -> tuple[ToolExecutor, _FakeAuth]:
    auth = _FakeAuth(token=token, refresh_token=refresh_token)
    executor = ToolExecutor(
        google_auth_factory=lambda _session: auth,
        calendar_service_factory=lambda _token, _calendar_id: calendar,
    )
    return executor, auth


# --- calendar_create -------------------------------------------------------


async def test_calendar_create_happy_path_creates_event_and_reference() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        create_returns={"id": "evt-123", "htmlLink": "https://cal/evt-123"}
    )
    executor, _ = _build_executor(token="ya29.fresh", calendar=calendar)

    result = await executor.execute(
        "calendar_create",
        {
            "title": "Guitar",
            "start": "2026-05-13T16:00:00+02:00",
            "end": "2026-05-13T17:00:00+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "Created: Guitar" in result.message
    assert "Wed 13 May, 16:00-17:00" in result.message
    assert calendar.create_calls[0]["title"] == "Guitar"
    assert calendar.create_calls[0]["timezone"] == "Europe/Madrid"
    # An EventReference was added to the session.
    assert any(
        getattr(obj, "google_event_id", None) == "evt-123"
        for obj in session.added
    )


async def test_calendar_create_invalid_datetime_returns_clarification() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(create_returns={"id": "evt"})
    executor, _ = _build_executor(token="ya29.fresh", calendar=calendar)

    result = await executor.execute(
        "calendar_create",
        {
            "title": "Guitar",
            "start": "tomorrow at 4pm",
            "end": "tomorrow at 5pm",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "didn't look right" in result.message
    assert calendar.create_calls == []


async def test_calendar_create_no_token_returns_authorize_link() -> None:
    user = _DummyUser()
    session = _FakeSession(None)
    calendar = _FakeCalendar()
    executor, _ = _build_executor(token=None, calendar=calendar)

    result = await executor.execute(
        "calendar_create",
        {
            "title": "Guitar",
            "start": "2026-05-13T16:00:00+02:00",
            "end": "2026-05-13T17:00:00+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "Authorize here:" in result.message


async def test_calendar_create_rate_limit_returns_friendly_reply() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        create_raises=[GoogleCalendarRateLimited(429, "rate limited")],
    )
    executor, _ = _build_executor(token="ya29.fresh", calendar=calendar)

    result = await executor.execute(
        "calendar_create",
        {
            "title": "x",
            "start": "2026-05-13T16:00:00+02:00",
            "end": "2026-05-13T17:00:00+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "busy" in result.message


async def test_calendar_create_401_triggers_refresh_and_retry() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        create_returns={"id": "evt-after-refresh"},
        create_raises=[GoogleCalendarUnauthorized(401, "expired")],
    )
    executor, auth = _build_executor(
        token="ya29.expired", refresh_token="ya29.fresh", calendar=calendar
    )

    result = await executor.execute(
        "calendar_create",
        {
            "title": "Guitar",
            "start": "2026-05-13T16:00:00+02:00",
            "end": "2026-05-13T17:00:00+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert auth.refresh_calls == 1
    assert len(calendar.create_calls) == 2


# --- calendar_query --------------------------------------------------------


async def test_calendar_query_formats_event_list() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        list_returns=[
            {
                "summary": "Standup",
                "start": {"dateTime": "2026-05-13T09:00:00+02:00"},
                "end": {"dateTime": "2026-05-13T10:00:00+02:00"},
            },
            {
                "summary": "Dentist",
                "start": {"dateTime": "2026-05-13T14:00:00+02:00"},
                "end": {"dateTime": "2026-05-13T15:30:00+02:00"},
            },
        ],
    )
    executor, _ = _build_executor(token="ya29.fresh", calendar=calendar)

    result = await executor.execute(
        "calendar_query",
        {
            "start_date": "2026-05-13T00:00:00+02:00",
            "end_date": "2026-05-13T23:59:59+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "Events for Wednesday 13 May" in result.message
    assert "09:00-10:00" in result.message
    assert "Standup" in result.message
    assert "14:00-15:30" in result.message
    assert "Dentist" in result.message


async def test_calendar_query_no_events_message() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(list_returns=[])
    executor, _ = _build_executor(token="ya29.fresh", calendar=calendar)

    result = await executor.execute(
        "calendar_query",
        {
            "start_date": "2026-05-13T00:00:00+02:00",
            "end_date": "2026-05-13T23:59:59+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == "No events found for Wednesday 13 May."


async def test_calendar_query_missing_args_asks_clarification() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    executor, _ = _build_executor(token="ya29.fresh", calendar=_FakeCalendar())

    result = await executor.execute(
        "calendar_query",
        {},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "date range" in result.message
