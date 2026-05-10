"""Execute LLM-planned tool calls and return user-facing WhatsApp messages."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import EventReference, GoogleAccount, User
from app.services.google_auth import GoogleAuthService, build_authorization_url
from app.services.google_calendar import (
    GoogleCalendarError,
    GoogleCalendarRateLimited,
    GoogleCalendarService,
    GoogleCalendarUnauthorized,
)
from app.utils.timezone import (
    InvalidDateTimeError,
    format_date_range,
    format_event_time,
    parse_iso_datetime,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Outcome of a tool execution. ``message`` is sent to the user verbatim."""

    success: bool
    message: str
    data: dict[str, Any] | None = None


@dataclass
class ToolExecutionLog:
    """Aggregated outcome for one inbound message: replies + per-tool data."""

    replies: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def combined_reply(self) -> str:
        cleaned = [reply for reply in self.replies if reply]
        if cleaned:
            return "\n".join(cleaned)
        return "I couldn't process that. Please try again."


class ToolExecutor:
    """Routes planner tool calls to handlers and returns ``ToolResult`` objects."""

    def __init__(
        self,
        google_auth_factory=None,
        calendar_service_factory=None,
    ) -> None:
        self._google_auth_factory = google_auth_factory or (
            lambda session: GoogleAuthService(session)
        )
        self._calendar_service_factory = calendar_service_factory or (
            lambda token, calendar_id: GoogleCalendarService(
                access_token=token, calendar_id=calendar_id
            )
        )

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user: User,
        db: AsyncSession,
        inbound_message_id: uuid.UUID | None = None,
    ) -> ToolResult:
        handler = {
            "reply": self.handle_reply,
            "ask_clarification": self.handle_ask_clarification,
            "calendar_create": self.handle_calendar_create,
            "calendar_query": self.handle_calendar_query,
        }.get(tool_name)

        if handler is None:
            return ToolResult(
                success=False,
                message=f"Tool {tool_name} not yet implemented.",
                data={"tool": tool_name, "reason": "not_implemented"},
            )

        try:
            return await handler(
                arguments, user=user, db=db, inbound_message_id=inbound_message_id
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Tool handler %s raised unexpectedly: %s", tool_name, exc
            )
            return ToolResult(
                success=False,
                message="Something went wrong. Please try again.",
                data={"tool": tool_name, "error": str(exc)},
            )

    async def handle_reply(
        self, arguments: dict[str, Any], **_: Any
    ) -> ToolResult:
        message = str(arguments.get("message") or "").strip()
        if not message:
            return ToolResult(success=False, message="", data={"tool": "reply"})
        return ToolResult(success=True, message=message, data={"tool": "reply"})

    async def handle_ask_clarification(
        self, arguments: dict[str, Any], **_: Any
    ) -> ToolResult:
        question = str(arguments.get("question") or "").strip()
        if not question:
            question = "Can you clarify?"
        return ToolResult(
            success=True, message=question, data={"tool": "ask_clarification"}
        )

    async def handle_calendar_create(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        inbound_message_id: uuid.UUID | None = None,
    ) -> ToolResult:
        title = str(arguments.get("title") or "").strip()
        start = arguments.get("start")
        end = arguments.get("end")
        description = arguments.get("description")

        if not title or not start or not end:
            return ToolResult(
                success=False,
                message="I need a title, start, and end to create that event.",
                data={"tool": "calendar_create", "reason": "missing_args"},
            )

        try:
            parse_iso_datetime(start)
            parse_iso_datetime(end)
        except InvalidDateTimeError:
            return ToolResult(
                success=False,
                message="That date didn't look right — when should I schedule it?",
                data={"tool": "calendar_create", "reason": "invalid_datetime"},
            )

        token, account = await self._get_token_and_account(user, db)
        if token is None or account is None:
            return self._unauthorized_result("calendar_create", user)

        timezone = user.timezone or get_settings().default_timezone
        event = await self._call_with_refresh(
            user=user,
            db=db,
            account=account,
            token=token,
            action=lambda access_token: self._calendar_service_factory(
                access_token, account.calendar_id
            ).create_event(
                title=title,
                start=start,
                end=end,
                description=description if isinstance(description, str) else None,
                timezone=timezone,
            ),
        )

        if isinstance(event, ToolResult):
            return event

        google_event_id = event.get("id")
        if google_event_id:
            db.add(
                EventReference(
                    user_id=user.id,
                    google_calendar_id=account.calendar_id,
                    google_event_id=google_event_id,
                    created_from_message_id=inbound_message_id,
                )
            )
            await db.flush()

        formatted = format_event_time(start, end, timezone)
        return ToolResult(
            success=True,
            message=f"Created: {title} - {formatted}",
            data={
                "tool": "calendar_create",
                "google_event_id": google_event_id,
                "html_link": event.get("htmlLink"),
            },
        )

    async def handle_calendar_query(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        **_: Any,
    ) -> ToolResult:
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")
        keywords = arguments.get("keywords")

        if not start_date or not end_date:
            return ToolResult(
                success=False,
                message="What date range should I check?",
                data={"tool": "calendar_query", "reason": "missing_args"},
            )

        try:
            parse_iso_datetime(start_date)
            parse_iso_datetime(end_date)
        except InvalidDateTimeError:
            return ToolResult(
                success=False,
                message="That date didn't look right — which day should I check?",
                data={"tool": "calendar_query", "reason": "invalid_datetime"},
            )

        token, account = await self._get_token_and_account(user, db)
        if token is None or account is None:
            return self._unauthorized_result("calendar_query", user)

        timezone = user.timezone or get_settings().default_timezone
        events = await self._call_with_refresh(
            user=user,
            db=db,
            account=account,
            token=token,
            action=lambda access_token: self._calendar_service_factory(
                access_token, account.calendar_id
            ).list_events(
                time_min=start_date,
                time_max=end_date,
                query=keywords if isinstance(keywords, str) and keywords else None,
            ),
        )

        if isinstance(events, ToolResult):
            return events

        header = format_date_range(start_date, end_date, timezone)
        if not events:
            return ToolResult(
                success=True,
                message=f"No events found for {header}.",
                data={"tool": "calendar_query", "count": 0},
            )

        lines = [f"Events for {header}:"]
        for event in events:
            lines.append(_format_event_line(event, timezone))
        return ToolResult(
            success=True,
            message="\n".join(lines),
            data={"tool": "calendar_query", "count": len(events)},
        )

    async def _get_token_and_account(
        self, user: User, db: AsyncSession
    ) -> tuple[str | None, GoogleAccount | None]:
        auth = self._google_auth_factory(db)
        token = await auth.get_valid_token(user.id)
        if token is None:
            return None, None
        # Re-read the account so we know the calendar_id; get_valid_token may
        # have refreshed the row already.
        from sqlalchemy import select

        result = await db.execute(
            select(GoogleAccount).where(GoogleAccount.user_id == user.id)
        )
        account = result.scalars().first()
        if account is None or account.status != "active":
            return None, None
        return token, account

    async def _call_with_refresh(
        self,
        *,
        user: User,
        db: AsyncSession,
        account: GoogleAccount,
        token: str,
        action,
    ) -> Any:
        """Run ``action(token)``; on 401 force-refresh once and retry."""
        try:
            return await action(token)
        except GoogleCalendarUnauthorized:
            logger.info(
                "Calendar 401 for user=%s — refreshing token and retrying", user.id
            )
        except GoogleCalendarRateLimited:
            return ToolResult(
                success=False,
                message="Calendar is busy, try again in a moment.",
                data={"reason": "rate_limited"},
            )
        except GoogleCalendarError:
            logger.exception("Calendar request failed for user=%s", user.id)
            return ToolResult(
                success=False,
                message="Could not reach Google Calendar. Please try again.",
                data={"reason": "calendar_error"},
            )

        # Retry path after 401
        auth = self._google_auth_factory(db)
        try:
            new_token = await auth.refresh_token(account)
        except Exception:
            logger.exception("Token refresh after 401 failed for user=%s", user.id)
            return self._unauthorized_result("calendar", user)

        try:
            return await action(new_token)
        except GoogleCalendarUnauthorized:
            return self._unauthorized_result("calendar", user)
        except GoogleCalendarRateLimited:
            return ToolResult(
                success=False,
                message="Calendar is busy, try again in a moment.",
                data={"reason": "rate_limited"},
            )
        except GoogleCalendarError:
            logger.exception(
                "Calendar request failed after refresh for user=%s", user.id
            )
            return ToolResult(
                success=False,
                message="Could not reach Google Calendar. Please try again.",
                data={"reason": "calendar_error"},
            )

    def _unauthorized_result(self, tool: str, user: User) -> ToolResult:
        url = build_authorization_url(user.wa_id)
        return ToolResult(
            success=False,
            message=(
                "I need access to your Google Calendar first. "
                f"Authorize here: {url}"
            ),
            data={"tool": tool, "reason": "not_authorized"},
        )


def _format_event_line(event: dict[str, Any], timezone: str) -> str:
    summary = str(event.get("summary") or "(no title)")
    start = (event.get("start") or {}).get("dateTime") or (
        event.get("start") or {}
    ).get("date")
    end = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get(
        "date"
    )
    if not start or not end:
        return f"- {summary}"
    try:
        time_part = format_event_time(start, end, timezone)
    except InvalidDateTimeError:
        return f"- {summary}"
    return f"- {time_part} - {summary}"
