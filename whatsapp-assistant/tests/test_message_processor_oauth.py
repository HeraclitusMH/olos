from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.models import GoogleAccount, User
from app.services.message_processor import (
    InboundMessage,
    _build_reply,
    _is_calendar_intent,
)


@pytest.mark.parametrize(
    "text",
    [
        "Schedule a meeting with John tomorrow",
        "What's on my calendar today?",
        "Book me a slot at 3pm",
        "Show my agenda",
        "Remind me to call Mom",
    ],
)
def test_is_calendar_intent_detects_keywords(text: str) -> None:
    assert _is_calendar_intent(text) is True


@pytest.mark.parametrize("text", ["hello", "echo me", "what's the weather?"])
def test_is_calendar_intent_ignores_unrelated_text(text: str) -> None:
    assert _is_calendar_intent(text) is False


class _AuthorizedSession:
    """Session whose `execute` returns an active GoogleAccount."""

    def __init__(self, account: object) -> None:
        self._account = account

    async def execute(self, _stmt: object) -> "_Result":
        return _Result(self._account)


class _NoAccountSession:
    async def execute(self, _stmt: object) -> "_Result":
        return _Result(None)


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalars(self) -> "_Result":
        return self

    def first(self) -> object:
        return self._value


async def test_build_reply_echoes_for_non_calendar_text() -> None:
    user = User(wa_id="34600111222")
    user.id = uuid.uuid4()
    session = _NoAccountSession()

    reply = await _build_reply(
        session,  # type: ignore[arg-type]
        user,
        InboundMessage(wa_id=user.wa_id, wa_message_id="m1", text="hi there"),
    )
    assert reply == "Echo: hi there"


async def test_build_reply_returns_auth_url_for_calendar_intent_when_unauthorized() -> None:
    user = User(wa_id="34600111222")
    user.id = uuid.uuid4()
    session = _NoAccountSession()

    reply = await _build_reply(
        session,  # type: ignore[arg-type]
        user,
        InboundMessage(
            wa_id=user.wa_id,
            wa_message_id="m1",
            text="Show me my calendar",
        ),
    )
    assert "accounts.google.com/o/oauth2/v2/auth" in reply
    state_param = parse_qs(urlparse(reply.splitlines()[-1]).query)["state"][0]
    assert state_param  # state was set
    assert "34600111222" not in state_param  # wa_id not leaked in state


async def test_build_reply_echoes_calendar_intent_when_authorized() -> None:
    user = User(wa_id="34600111222")
    user.id = uuid.uuid4()
    account = GoogleAccount(
        user_id=user.id,
        access_token_enc=b"_",
        refresh_token_enc=b"_",
        token_expires_at=datetime.now(UTC),
        status="active",
    )
    session = _AuthorizedSession(account)

    reply = await _build_reply(
        session,  # type: ignore[arg-type]
        user,
        InboundMessage(
            wa_id=user.wa_id,
            wa_message_id="m1",
            text="Schedule a meeting",
        ),
    )
    assert reply == "Echo: Schedule a meeting"
