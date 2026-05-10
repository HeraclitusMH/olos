from __future__ import annotations

import uuid
from typing import Any

from app.models import EventReference, GoogleAccount
from app.services.event_resolver import ResolvedEvent
from app.services.tool_executor import ToolExecutor


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = {}


class _ScalarsResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any:
        return self._items[0] if self._items else None

    def all(self) -> list[Any]:
        return list(self._items)


class _ResultProxy:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _ScalarsResult:
        return _ScalarsResult(self._items)


class _FakeSession:
    """Fakes the bits of AsyncSession the executor touches.

    ``execute_results`` is a list of result-sets returned in FIFO order
    for successive ``execute()`` calls. Useful for sequencing the
    GoogleAccount lookup followed by message/event-reference queries.
    """

    def __init__(
        self,
        account: GoogleAccount | None,
        execute_results: list[list[Any]] | None = None,
    ) -> None:
        self._account = account
        self._extra_results = list(execute_results or [])
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt: Any) -> _ResultProxy:
        if self._extra_results:
            return _ResultProxy(self._extra_results.pop(0))
        return _ResultProxy([self._account] if self._account is not None else [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

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
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def get_valid_token(self, _user_id: uuid.UUID) -> str | None:
        return self._token

    async def refresh_token(self, _account: GoogleAccount) -> str:  # pragma: no cover
        raise RuntimeError("not used")


class _FakeCalendar:
    def __init__(
        self,
        *,
        list_returns: list[dict[str, Any]] | None = None,
        update_returns: dict[str, Any] | None = None,
        get_returns: dict[str, Any] | None = None,
        delete_returns: Any = None,
    ) -> None:
        self._list_returns = list(list_returns or [])
        self._update_returns = update_returns or {}
        self._get_returns = get_returns or {}
        self._delete_returns = delete_returns
        self.list_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.get_calls: list[str] = []

    async def list_events(
        self, time_min: str, time_max: str, query: str | None = None, max_results: int = 20
    ) -> list[dict[str, Any]]:
        self.list_calls.append(
            {"time_min": time_min, "time_max": time_max, "query": query}
        )
        return list(self._list_returns)

    async def update_event(
        self, event_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        self.update_calls.append({"event_id": event_id, "updates": updates})
        return self._update_returns

    async def delete_event(self, event_id: str) -> None:
        self.delete_calls.append(event_id)
        return None

    async def get_event(self, event_id: str) -> dict[str, Any]:
        self.get_calls.append(event_id)
        return self._get_returns


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
            {"to": to, "button_text": button_text, "sections": sections}
        )
        return {"messages": [{"id": "wamid.OUT"}]}


def _build_executor(
    *,
    token: str | None,
    calendar: _FakeCalendar,
    whatsapp_client: _FakeWhatsAppClient | None = None,
) -> ToolExecutor:
    auth = _FakeAuth(token=token)
    return ToolExecutor(
        google_auth_factory=lambda _session: auth,
        calendar_service_factory=lambda _token, _calendar_id: calendar,
        whatsapp_client_factory=(lambda: whatsapp_client) if whatsapp_client else None,
    )


def _gcal_event(
    *,
    event_id: str,
    summary: str,
    start: str = "2026-05-13T16:00:00+02:00",
    end: str = "2026-05-13T17:00:00+02:00",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


# --- calendar_update -------------------------------------------------------


async def test_update_single_match_updates_and_confirms() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        list_returns=[_gcal_event(event_id="evt-1", summary="Guitar practice")],
        update_returns={"id": "evt-1"},
    )
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_update",
        {"search_title": "guitar", "new_start": "2026-05-13T17:00:00+02:00",
         "new_end": "2026-05-13T18:00:00+02:00"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "Updated: Guitar practice" in result.message
    assert "17:00-18:00" in result.message
    assert calendar.update_calls[0]["event_id"] == "evt-1"
    updates = calendar.update_calls[0]["updates"]
    assert updates["start"]["dateTime"] == "2026-05-13T17:00:00+02:00"
    assert updates["end"]["dateTime"] == "2026-05-13T18:00:00+02:00"


async def test_update_no_changes_asks_clarification() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar()
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_update",
        {"search_title": "guitar"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "What should I change" in result.message
    assert calendar.list_calls == []


async def test_update_no_match_returns_not_found() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(list_returns=[])
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_update",
        {"search_title": "ghost", "new_start": "2026-05-13T17:00:00+02:00",
         "new_end": "2026-05-13T18:00:00+02:00"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "couldn't find" in result.message
    assert "'ghost'" in result.message


async def test_update_multiple_matches_sends_buttons_and_stores_pending() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        list_returns=[
            _gcal_event(event_id="evt-1", summary="Team meeting"),
            _gcal_event(
                event_id="evt-2",
                summary="Project meeting",
                start="2026-05-14T10:00:00+02:00",
                end="2026-05-14T11:00:00+02:00",
            ),
        ],
    )
    whatsapp = _FakeWhatsAppClient()
    executor = _build_executor(
        token="ya29", calendar=calendar, whatsapp_client=whatsapp
    )

    result = await executor.execute(
        "calendar_update",
        {"search_title": "meeting", "new_start": "2026-05-13T17:00:00+02:00",
         "new_end": "2026-05-13T18:00:00+02:00"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == ""
    assert (result.data or {}).get("sent_directly") is True
    assert len(whatsapp.button_calls) == 1
    assert calendar.update_calls == []

    pending = user.preferences_json["pending_action"]
    assert pending["type"] == "calendar_update"
    assert pending["id"] == result.data["disambiguation_id"]
    assert {opt["event_id"] for opt in pending["options"]} == {"evt-1", "evt-2"}


async def test_update_with_preselected_event_skips_resolution() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(update_returns={"id": "evt-9"})
    executor = _build_executor(token="ya29", calendar=calendar)

    preselected = ResolvedEvent(
        event_id="evt-9",
        title="Project meeting",
        start="2026-05-14T10:00:00+02:00",
        end="2026-05-14T11:00:00+02:00",
        calendar_id="primary",
    )

    result = await executor.execute(
        "calendar_update",
        {"search_title": "meeting", "new_start": "2026-05-14T11:00:00+02:00",
         "new_end": "2026-05-14T12:00:00+02:00"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
        preselected_event=preselected,
    )

    assert result.success is True
    assert calendar.list_calls == []
    assert calendar.update_calls[0]["event_id"] == "evt-9"


async def test_update_invalid_new_start_returns_clarification() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar()
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_update",
        {"search_title": "x", "new_start": "tomorrow", "new_end": "later"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "didn't look right" in result.message


# --- calendar_cancel -------------------------------------------------------


async def test_cancel_single_match_deletes_and_confirms() -> None:
    user = _DummyUser()
    account = _make_account(user.id)
    existing_ref = EventReference(
        user_id=user.id,
        google_calendar_id="primary",
        google_event_id="evt-1",
    )
    # First execute() -> account; second -> EventReference rows.
    session = _FakeSession(account, execute_results=[[account], [existing_ref]])
    calendar = _FakeCalendar(
        list_returns=[_gcal_event(event_id="evt-1", summary="Dentist")]
    )
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_cancel",
        {"search_title": "dentist"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "Cancelled: Dentist" in result.message
    assert calendar.delete_calls == ["evt-1"]
    assert existing_ref in session.deleted


async def test_cancel_no_match_returns_not_found() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(list_returns=[])
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_cancel",
        {"search_title": "ghost"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "couldn't find" in result.message
    assert calendar.delete_calls == []


async def test_cancel_multiple_matches_sends_buttons() -> None:
    user = _DummyUser()
    session = _FakeSession(_make_account(user.id))
    calendar = _FakeCalendar(
        list_returns=[
            _gcal_event(event_id="evt-1", summary="Team meeting"),
            _gcal_event(
                event_id="evt-2",
                summary="Project meeting",
                start="2026-05-14T10:00:00+02:00",
                end="2026-05-14T11:00:00+02:00",
            ),
        ],
    )
    whatsapp = _FakeWhatsAppClient()
    executor = _build_executor(
        token="ya29", calendar=calendar, whatsapp_client=whatsapp
    )

    result = await executor.execute(
        "calendar_cancel",
        {"search_title": "meeting"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == ""
    assert calendar.delete_calls == []
    assert len(whatsapp.button_calls) == 1
    assert user.preferences_json["pending_action"]["type"] == "calendar_cancel"


async def test_cancel_with_preselected_event_deletes_directly() -> None:
    user = _DummyUser()
    account = _make_account(user.id)
    session = _FakeSession(account, execute_results=[[account], []])
    calendar = _FakeCalendar()
    executor = _build_executor(token="ya29", calendar=calendar)

    preselected = ResolvedEvent(
        event_id="evt-2",
        title="Project meeting",
        start="2026-05-14T10:00:00+02:00",
        end="2026-05-14T11:00:00+02:00",
        calendar_id="primary",
    )

    result = await executor.execute(
        "calendar_cancel",
        {"search_title": "meeting"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
        preselected_event=preselected,
    )

    assert result.success is True
    assert "Cancelled: Project meeting" in result.message
    assert calendar.delete_calls == ["evt-2"]


# --- vague title context resolution ----------------------------------------


async def test_update_vague_title_resolves_from_recent_message_context() -> None:
    user = _DummyUser()
    account = _make_account(user.id)

    class _Msg:
        execution_result_json = {
            "results": [
                {
                    "tool": "calendar_create",
                    "data": {"google_event_id": "evt-context"},
                }
            ]
        }

    # First execute -> account, second -> recent messages
    session = _FakeSession(account, execute_results=[[account], [_Msg()]])
    calendar = _FakeCalendar(
        get_returns=_gcal_event(event_id="evt-context", summary="Guitar"),
        update_returns={"id": "evt-context"},
    )
    executor = _build_executor(token="ya29", calendar=calendar)

    result = await executor.execute(
        "calendar_update",
        {"search_title": "it", "new_start": "2026-05-13T17:00:00+02:00",
         "new_end": "2026-05-13T18:00:00+02:00"},
        user=user,  # type: ignore[arg-type]
        db=session,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert calendar.get_calls == ["evt-context"]
    assert calendar.list_calls == []
    assert calendar.update_calls[0]["event_id"] == "evt-context"
