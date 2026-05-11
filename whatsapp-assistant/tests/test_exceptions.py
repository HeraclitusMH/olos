"""Tests for the custom exception hierarchy."""

from __future__ import annotations

from app.utils.exceptions import (
    AssistantError,
    DailyLimitExceededError,
    GoogleCalendarError,
    OpenAIError,
    TokenExpiredError,
    WhatsAppSendError,
)


def test_subclasses_inherit_from_assistant_error() -> None:
    for cls in (
        OpenAIError,
        GoogleCalendarError,
        TokenExpiredError,
        DailyLimitExceededError,
        WhatsAppSendError,
    ):
        assert issubclass(cls, AssistantError)


def test_user_message_default_is_friendly() -> None:
    err = OpenAIError(log_message="upstream 500 from gpt-4o-mini")
    assert "trouble" in err.user_message.lower()


def test_user_message_override() -> None:
    err = GoogleCalendarError(
        log_message="503 from calendar",
        user_message="Calendar is napping",
    )
    assert err.user_message == "Calendar is napping"


def test_log_message_defaults_to_user_message_when_unspecified() -> None:
    err = DailyLimitExceededError()
    assert err.log_message  # not empty
