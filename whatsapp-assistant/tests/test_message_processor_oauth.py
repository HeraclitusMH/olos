from __future__ import annotations

import uuid
from typing import Any

from app.services.tool_executor import ToolExecutor


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"


async def test_executor_routes_reply_tool_call() -> None:
    executor = ToolExecutor()
    result = await executor.execute(
        "reply",
        {"message": "Hi. How can I help?"},
        user=_DummyUser(),
        db=None,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == "Hi. How can I help?"


async def test_executor_routes_ask_clarification_tool_call() -> None:
    executor = ToolExecutor()
    result = await executor.execute(
        "ask_clarification",
        {"question": "What time should I schedule it?"},
        user=_DummyUser(),
        db=None,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == "What time should I schedule it?"


async def test_executor_returns_not_implemented_for_unknown_tool() -> None:
    executor = ToolExecutor()
    result = await executor.execute(
        "memory_store",
        {"content": "x", "tags": ["a", "b"]},
        user=_DummyUser(),
        db=None,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.message == "Tool memory_store not yet implemented."


async def test_executor_unauthorized_returns_authorize_link() -> None:
    class _NoTokenAuth:
        def __init__(self, _session: Any) -> None:
            pass

        async def get_valid_token(self, _user_id: uuid.UUID) -> str | None:
            return None

    executor = ToolExecutor(google_auth_factory=lambda session: _NoTokenAuth(session))
    user = _DummyUser()
    result = await executor.execute(
        "calendar_create",
        {
            "title": "Guitar",
            "start": "2026-05-13T16:00:00+02:00",
            "end": "2026-05-13T17:00:00+02:00",
        },
        user=user,  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "Authorize here:" in result.message
    assert "https://accounts.google.com/o/oauth2/v2/auth" in result.message
