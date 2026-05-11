"""Custom exception hierarchy for the assistant.

Each exception carries two strings:

* ``user_message`` — short, WhatsApp-safe text shown to the user.
* ``log_message`` — detailed context for logs (may include status codes etc.).
"""

from __future__ import annotations


class AssistantError(Exception):
    """Base class for all assistant errors.

    Concrete subclasses default ``user_message`` to a friendly canned string so
    the message processor can simply send ``exc.user_message`` to the user
    without per-call-site decisions.
    """

    default_user_message = "Something went wrong. Please try again."

    def __init__(
        self,
        log_message: str = "",
        *,
        user_message: str | None = None,
    ) -> None:
        self.log_message = log_message or self.default_user_message
        self.user_message = user_message or self.default_user_message
        super().__init__(self.log_message)


class OpenAIError(AssistantError):
    """Failure communicating with the OpenAI planner."""

    default_user_message = "Having trouble thinking right now. Try again in a minute."


class GoogleCalendarError(AssistantError):
    """Failure communicating with Google Calendar."""

    default_user_message = "Calendar temporarily unavailable."


class TokenExpiredError(AssistantError):
    """Google OAuth refresh permanently failed — re-authorization required."""

    default_user_message = (
        "Your Google connection expired. Please re-authorize to continue."
    )


class DailyLimitExceededError(AssistantError):
    """The user (or the assistant) has hit its daily OpenAI request budget."""

    default_user_message = "Hit my daily limit. Back tomorrow!"


class WhatsAppSendError(AssistantError):
    """Failure sending an outbound WhatsApp message."""

    default_user_message = ""
