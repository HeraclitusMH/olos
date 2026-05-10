from __future__ import annotations

import uuid
from typing import Any

from app.services.event_resolver import ResolvedEvent
from app.services.message_processor import _process_interactive_reply
from app.services.pending_action import set_pending_action
from app.services.tool_executor import ToolExecutor, ToolResult


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = {}


class _NoOpSession:
    """The interactive-reply path delegates DB work to the executor; we fake it."""

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover - not exercised
        raise AssertionError("session not used directly")

    def add(self, _obj: Any) -> None:  # pragma: no cover
        pass

    async def flush(self) -> None:  # pragma: no cover
        pass


class _StubExecutor(ToolExecutor):
    """Executor that records and returns canned ToolResults."""

    def __init__(self, response: ToolResult) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def execute(  # type: ignore[override]
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user: Any,
        db: Any,
        inbound_message_id: Any = None,
        preselected_event: ResolvedEvent | None = None,
    ) -> ToolResult:
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "preselected_event": preselected_event,
            }
        )
        return self.response


def _selected_event() -> ResolvedEvent:
    return ResolvedEvent(
        event_id="evt-2",
        title="Project meeting",
        start="2026-05-14T10:00:00+02:00",
        end="2026-05-14T11:00:00+02:00",
        calendar_id="primary",
    )


def _options() -> list[ResolvedEvent]:
    return [
        ResolvedEvent(
            event_id="evt-1",
            title="Team meeting",
            start="2026-05-13T16:00:00+02:00",
            end="2026-05-13T17:00:00+02:00",
            calendar_id="primary",
        ),
        _selected_event(),
    ]


async def test_button_reply_executes_pending_action_and_clears_state() -> None:
    user = _DummyUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_cancel",
        arguments={"search_title": "meeting"},
        options=_options(),
    )
    executor = _StubExecutor(
        ToolResult(
            success=True,
            message="Cancelled: Project meeting - Thu 14 May, 10:00-11:00",
            data={"tool": "calendar_cancel"},
        )
    )

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:1",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "Cancelled: Project meeting" in log.combined_reply
    assert executor.calls[0]["tool_name"] == "calendar_cancel"
    assert executor.calls[0]["preselected_event"].event_id == "evt-2"
    assert "pending_action" not in user.preferences_json


async def test_button_reply_with_unknown_id_is_ignored() -> None:
    user = _DummyUser()
    executor = _StubExecutor(ToolResult(success=True, message="ok"))

    log = await _process_interactive_reply(
        interactive_id="not-our-prefix",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "don't have a pending request" in log.combined_reply
    assert executor.calls == []


async def test_button_reply_with_expired_pending_returns_message() -> None:
    user = _DummyUser()
    # Manually inject a stale pending action.
    user.preferences_json = {
        "pending_action": {
            "id": "abc",
            "type": "calendar_update",
            "arguments": {},
            "options": [],
            "expires_at": "2000-01-01T00:00:00+00:00",
        }
    }
    executor = _StubExecutor(ToolResult(success=True, message="ok"))

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:0",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "expired" in log.combined_reply.lower()
    assert executor.calls == []
    # Stale pending action should be cleared on access.
    assert "pending_action" not in user.preferences_json


async def test_button_reply_with_mismatched_disambiguation_id_returns_expired() -> None:
    user = _DummyUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_cancel",
        arguments={},
        options=_options(),
    )
    executor = _StubExecutor(ToolResult(success=True, message="ok"))

    log = await _process_interactive_reply(
        interactive_id="disambig:zzz:0",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "expired" in log.combined_reply.lower()
    assert executor.calls == []


async def test_button_reply_with_out_of_range_index_returns_invalid() -> None:
    user = _DummyUser()
    set_pending_action(
        user,  # type: ignore[arg-type]
        disambiguation_id="abc",
        action_type="calendar_cancel",
        arguments={},
        options=_options(),
    )
    executor = _StubExecutor(ToolResult(success=True, message="ok"))

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:99",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=executor,
    )

    assert "no longer valid" in log.combined_reply.lower()
    assert executor.calls == []
