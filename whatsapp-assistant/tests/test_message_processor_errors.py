"""Error-mapping tests for the message processor's global error handler."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services.message_processor import (
    _error_reply,
    _process_text_message,
)
from app.services.tool_executor import ToolExecutor
from app.utils.exceptions import (
    DailyLimitExceededError,
    GoogleCalendarError,
    OpenAIError,
    TokenExpiredError,
    WhatsAppSendError,
)


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"


class _CountingCostTracker:
    """``check_limit`` returns False to simulate the daily budget being hit."""

    def __init__(self, *, over_limit: bool) -> None:
        self.over_limit = over_limit
        self.increments = 0

    async def check_limit(self, _db: Any) -> bool:
        return not self.over_limit

    async def increment(self, _db: Any) -> None:
        self.increments += 1


class _FakeMessage:
    def __init__(self) -> None:
        self.tool_calls_json: dict | None = None


class _FakeSession:
    async def execute(self, _stmt: Any) -> Any:
        class _R:
            def scalars(self_inner):
                return self_inner

            def all(self_inner) -> list:
                return []

        return _R()


# --- _error_reply ----------------------------------------------------------


def test_error_reply_daily_limit() -> None:
    reply = _error_reply(DailyLimitExceededError(), _DummyUser())
    assert reply == "Hit my daily limit. Back tomorrow!"


def test_error_reply_openai_error() -> None:
    reply = _error_reply(OpenAIError("upstream 500"), _DummyUser())
    assert "trouble" in reply.lower()


def test_error_reply_token_expired_includes_reauth_link() -> None:
    user = _DummyUser()
    reply = _error_reply(TokenExpiredError("revoked"), user)
    assert "re-authorize" in reply.lower()
    assert "accounts.google.com" in reply


def test_error_reply_google_calendar() -> None:
    reply = _error_reply(GoogleCalendarError("503"), _DummyUser())
    assert reply == "Calendar temporarily unavailable."


def test_error_reply_generic_exception() -> None:
    reply = _error_reply(RuntimeError("boom"), _DummyUser())
    assert reply == "Something went wrong."


def test_error_reply_whatsapp_send_error_is_caught_via_assistant_error() -> None:
    # WhatsAppSendError is an AssistantError subclass but isn't specifically
    # mapped — falls through to the generic message.
    reply = _error_reply(WhatsAppSendError("400"), _DummyUser())
    assert reply == "Something went wrong."


# --- _process_text_message: surfaces DailyLimitExceededError --------------


async def test_process_text_message_raises_daily_limit() -> None:
    from app.services.message_processor import InboundMessage

    user = _DummyUser()
    cost_tracker = _CountingCostTracker(over_limit=True)

    class _UnusedPlanner:
        async def plan(self, *_args, **_kwargs):
            raise AssertionError("planner must not be called when over limit")

    inbound_msg = _FakeMessage()
    inbound = InboundMessage(
        wa_id=user.wa_id, wa_message_id="wamid.X", text="hi"
    )

    with pytest.raises(DailyLimitExceededError):
        await _process_text_message(
            inbound=inbound,
            user=user,  # type: ignore[arg-type]
            session=_FakeSession(),  # type: ignore[arg-type]
            inbound_message_id=uuid.uuid4(),
            inbound_msg=inbound_msg,  # type: ignore[arg-type]
            cost_tracker=cost_tracker,  # type: ignore[arg-type]
            planner=_UnusedPlanner(),  # type: ignore[arg-type]
            tool_executor=ToolExecutor(),
        )

    assert cost_tracker.increments == 0
