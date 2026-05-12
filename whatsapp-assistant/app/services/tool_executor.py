"""Execute LLM-planned tool calls and return user-facing WhatsApp messages."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import EventReference, GoogleAccount, Memory, Message, User
from app.services.disambiguation import (
    DisambiguationService,
    build_option_id,
)
from app.services.event_resolver import EventResolver, ResolvedEvent, is_vague_title
from app.services.google_auth import GoogleAuthService, build_authorization_url
from app.services.google_calendar import (
    GoogleCalendarError,
    GoogleCalendarRateLimited,
    GoogleCalendarService,
    GoogleCalendarUnauthorized,
)
from app.services.memory import MemoryService
from app.services.pending_action import (
    clear_pending_action,
    set_pending_action,
)
from app.services.reminder import ReminderError, ReminderService
from app.services.whatsapp import WhatsAppClient
from app.utils.exceptions import AssistantError
from app.utils.search import generate_tags
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
    sent_directly: bool = False

    @property
    def combined_reply(self) -> str:
        cleaned = [reply for reply in self.replies if reply]
        if cleaned:
            return "\n".join(cleaned)
        if self.sent_directly:
            # An interactive prompt was already sent — no text reply needed.
            return ""
        return "I couldn't process that. Please try again."


class ToolExecutor:
    """Routes planner tool calls to handlers and returns ``ToolResult`` objects."""

    def __init__(
        self,
        google_auth_factory=None,
        calendar_service_factory=None,
        whatsapp_client_factory=None,
        disambiguation_service_factory=None,
        memory_service_factory=None,
        reminder_service_factory=None,
    ) -> None:
        self._google_auth_factory = google_auth_factory or (
            lambda session: GoogleAuthService(session)
        )
        self._calendar_service_factory = calendar_service_factory or (
            lambda token, calendar_id: GoogleCalendarService(
                access_token=token, calendar_id=calendar_id
            )
        )
        self._whatsapp_client_factory = whatsapp_client_factory or WhatsAppClient
        self._disambiguation_service_factory = (
            disambiguation_service_factory
            or (lambda client: DisambiguationService(client))
        )
        self._memory_service_factory = memory_service_factory or MemoryService
        self._reminder_service_factory = reminder_service_factory or ReminderService

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user: User,
        db: AsyncSession,
        inbound_message_id: uuid.UUID | None = None,
        preselected_event: ResolvedEvent | None = None,
    ) -> ToolResult:
        handler = {
            "reply": self.handle_reply,
            "ask_clarification": self.handle_ask_clarification,
            "calendar_create": self.handle_calendar_create,
            "calendar_query": self.handle_calendar_query,
            "calendar_update": self.handle_calendar_update,
            "calendar_cancel": self.handle_calendar_cancel,
            "memory_store": self.handle_memory_store,
            "memory_retrieve": self.handle_memory_retrieve,
            "memory_update": self.handle_memory_update,
            "memory_forget": self.handle_memory_forget,
            "reminder_create": self.handle_reminder_create,
        }.get(tool_name)

        if handler is None:
            return ToolResult(
                success=False,
                message=f"Tool {tool_name} not yet implemented.",
                data={"tool": tool_name, "reason": "not_implemented"},
            )

        try:
            return await handler(
                arguments,
                user=user,
                db=db,
                inbound_message_id=inbound_message_id,
                preselected_event=preselected_event,
            )
        except AssistantError:
            # Surface domain errors (TokenExpiredError, GoogleCalendarError, …)
            # to the message processor so it can map them to user-facing replies.
            raise
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
        **_: Any,
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
                "title": title,
                "start": start,
                "end": end,
                "calendar_id": account.calendar_id,
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

    async def handle_calendar_update(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        inbound_message_id: uuid.UUID | None = None,
        preselected_event: ResolvedEvent | None = None,
        **_: Any,
    ) -> ToolResult:
        new_title = arguments.get("new_title")
        new_start = arguments.get("new_start")
        new_end = arguments.get("new_end")
        new_description = arguments.get("new_description")

        if not any(
            isinstance(value, str) and value
            for value in (new_title, new_start, new_end, new_description)
        ):
            return ToolResult(
                success=False,
                message="What should I change about it?",
                data={"tool": "calendar_update", "reason": "no_changes"},
            )

        for value in (new_start, new_end):
            if isinstance(value, str) and value:
                try:
                    parse_iso_datetime(value)
                except InvalidDateTimeError:
                    return ToolResult(
                        success=False,
                        message="That date didn't look right — when should I move it to?",
                        data={
                            "tool": "calendar_update",
                            "reason": "invalid_datetime",
                        },
                    )

        token, account = await self._get_token_and_account(user, db)
        if token is None or account is None:
            return self._unauthorized_result("calendar_update", user)

        if preselected_event is not None:
            return await self._apply_calendar_update(
                event=preselected_event,
                arguments=arguments,
                user=user,
                db=db,
                account=account,
                token=token,
            )

        resolution = await self._resolve_event(
            arguments=arguments, user=user, db=db, account=account, token=token
        )
        if isinstance(resolution, ToolResult):
            return resolution
        if resolution is None:
            return _not_found_result("calendar_update", arguments)
        if isinstance(resolution, list):
            return await self._send_disambiguation(
                action_type="calendar_update",
                action_label="update",
                arguments=arguments,
                user=user,
                db=db,
                events=resolution,
            )

        return await self._apply_calendar_update(
            event=resolution,
            arguments=arguments,
            user=user,
            db=db,
            account=account,
            token=token,
        )

    async def handle_calendar_cancel(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        inbound_message_id: uuid.UUID | None = None,
        preselected_event: ResolvedEvent | None = None,
        **_: Any,
    ) -> ToolResult:
        token, account = await self._get_token_and_account(user, db)
        if token is None or account is None:
            return self._unauthorized_result("calendar_cancel", user)

        if preselected_event is not None:
            return await self._apply_calendar_cancel(
                event=preselected_event,
                user=user,
                db=db,
                account=account,
                token=token,
            )

        resolution = await self._resolve_event(
            arguments=arguments, user=user, db=db, account=account, token=token
        )
        if isinstance(resolution, ToolResult):
            return resolution
        if resolution is None:
            return _not_found_result("calendar_cancel", arguments)
        if isinstance(resolution, list):
            return await self._send_disambiguation(
                action_type="calendar_cancel",
                action_label="cancel",
                arguments=arguments,
                user=user,
                db=db,
                events=resolution,
            )

        return await self._apply_calendar_cancel(
            event=resolution,
            user=user,
            db=db,
            account=account,
            token=token,
        )

    # --- memory handlers ---------------------------------------------------

    async def handle_memory_store(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        **_: Any,
    ) -> ToolResult:
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return ToolResult(
                success=False,
                message="What should I remember?",
                data={"tool": "memory_store", "reason": "missing_content"},
            )
        raw_tags = arguments.get("tags") or []
        tags: list[str] = []
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                if isinstance(tag, str) and tag.strip():
                    tags.append(tag.strip().lower())

        memory_service = self._memory_service_factory()
        memory = await memory_service.store(
            user_id=user.id, content=content.strip(), tags=tags or None, db=db
        )

        return ToolResult(
            success=True,
            message=f"Saved: {_summarize(memory.content)}",
            data={
                "tool": "memory_store",
                "memory_id": str(memory.id),
                "tags": list(memory.tags or []),
            },
        )

    async def handle_memory_retrieve(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        **_: Any,
    ) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                success=False,
                message="What should I look up?",
                data={"tool": "memory_retrieve", "reason": "missing_query"},
            )

        memory_service = self._memory_service_factory()
        matches = await memory_service.retrieve(
            user_id=user.id, query=query, db=db, limit=3
        )
        if not matches:
            return ToolResult(
                success=True,
                message="I do not have anything stored about that.",
                data={"tool": "memory_retrieve", "count": 0},
            )
        if len(matches) == 1:
            return ToolResult(
                success=True,
                message=matches[0].content,
                data={
                    "tool": "memory_retrieve",
                    "count": 1,
                    "memory_id": str(matches[0].id),
                },
            )
        lines = [f"{idx}. {m.content}" for idx, m in enumerate(matches, 1)]
        return ToolResult(
            success=True,
            message="\n".join(lines),
            data={
                "tool": "memory_retrieve",
                "count": len(matches),
                "memory_ids": [str(m.id) for m in matches],
            },
        )

    async def handle_memory_update(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        **_: Any,
    ) -> ToolResult:
        query = arguments.get("query")
        new_content = arguments.get("new_content")
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(new_content, str)
            or not new_content.strip()
        ):
            return ToolResult(
                success=False,
                message="What memory should I update, and to what?",
                data={"tool": "memory_update", "reason": "missing_args"},
            )

        memory_service = self._memory_service_factory()
        matches = await memory_service.retrieve(
            user_id=user.id, query=query, db=db, limit=3
        )
        if not matches:
            return ToolResult(
                success=False,
                message="Could not find a memory matching that.",
                data={"tool": "memory_update", "reason": "not_found"},
            )
        if len(matches) > 1:
            return await self._send_memory_update_disambiguation(
                user=user,
                db=db,
                memories=matches,
                query=query,
                new_content=new_content.strip(),
            )

        memory = matches[0]
        old_summary = _summarize(memory.content)
        new_text = new_content.strip()
        new_tags = await generate_tags(new_text)
        await memory_service.update_content(
            memory=memory, new_content=new_text, new_tags=new_tags, db=db
        )
        return ToolResult(
            success=True,
            message=f"Updated: {old_summary} -> {_summarize(new_text)}",
            data={
                "tool": "memory_update",
                "memory_id": str(memory.id),
            },
        )

    async def handle_memory_forget(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        **_: Any,
    ) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                success=False,
                message="What should I forget?",
                data={"tool": "memory_forget", "reason": "missing_query"},
            )

        memory_service = self._memory_service_factory()
        matches = await memory_service.retrieve(
            user_id=user.id, query=query, db=db, limit=3
        )
        if not matches:
            return ToolResult(
                success=False,
                message="Could not find a memory matching that.",
                data={"tool": "memory_forget", "reason": "not_found"},
            )
        if len(matches) > 1:
            return await self._send_memory_forget_disambiguation(
                user=user, db=db, memories=matches, query=query
            )

        return await self._send_forget_confirmation(
            user=user, db=db, memory=matches[0]
        )

    async def handle_reminder_create(
        self,
        arguments: dict[str, Any],
        *,
        user: User,
        db: AsyncSession,
        **_: Any,
    ) -> ToolResult:
        reminder_text = arguments.get("reminder_text")
        remind_at_raw = arguments.get("remind_at")

        if (
            not isinstance(reminder_text, str)
            or not reminder_text.strip()
            or not isinstance(remind_at_raw, str)
            or not remind_at_raw.strip()
        ):
            return ToolResult(
                success=False,
                message="What should I remind you about, and when?",
                data={"tool": "reminder_create", "reason": "missing_args"},
            )

        try:
            remind_at = parse_iso_datetime(remind_at_raw)
        except InvalidDateTimeError:
            return ToolResult(
                success=False,
                message="That time didn't look right — when should I remind you?",
                data={"tool": "reminder_create", "reason": "invalid_datetime"},
            )

        if remind_at.tzinfo is None:
            timezone = user.timezone or get_settings().default_timezone
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                remind_at = remind_at.replace(tzinfo=ZoneInfo(timezone))
            except ZoneInfoNotFoundError:
                remind_at = remind_at.replace(
                    tzinfo=ZoneInfo(get_settings().default_timezone)
                )

        service = self._reminder_service_factory()
        try:
            reminder = await service.create_reminder(
                user_id=user.id,
                reminder_text=reminder_text.strip(),
                remind_at=remind_at,
                db=db,
            )
        except ReminderError as exc:
            return ToolResult(
                success=False,
                message=exc.user_message,
                data={"tool": "reminder_create", "reason": "rejected"},
            )

        timezone = user.timezone or get_settings().default_timezone
        try:
            formatted = format_event_time(
                remind_at.isoformat(), remind_at.isoformat(), timezone
            )
            # format_event_time returns "Tue 13 May, 09:30-09:30" — keep
            # only the day and start time for a reminder confirmation.
            day_part, _, time_range = formatted.partition(",")
            start_time = time_range.strip().split("-", 1)[0].strip()
            display = f"{day_part.strip()} at {start_time}".strip()
        except InvalidDateTimeError:
            display = remind_at.isoformat()

        return ToolResult(
            success=True,
            message=f"Reminder set for {display}.",
            data={
                "tool": "reminder_create",
                "reminder_id": str(reminder.id),
                "remind_at": remind_at.isoformat(),
            },
        )

    async def _send_forget_confirmation(
        self, *, user: User, db: AsyncSession, memory: Memory
    ) -> ToolResult:
        preview = _summarize(memory.content, length=80)
        body_text = f"Are you sure you want to forget: {preview}?"
        memory_id = str(memory.id)
        buttons = [
            {
                "type": "reply",
                "reply": {
                    "id": f"confirm_forget_{memory_id}",
                    "title": "Yes, forget it",
                },
            },
            {
                "type": "reply",
                "reply": {
                    "id": f"cancel_forget_{memory_id}",
                    "title": "No, keep it",
                },
            },
        ]
        client = self._whatsapp_client_factory()
        try:
            await client.send_interactive_buttons(
                to=user.wa_id, body_text=body_text, buttons=buttons
            )
        except Exception:
            logger.exception(
                "Failed to send forget confirmation for user=%s", user.id
            )
            return ToolResult(
                success=False,
                message="I couldn't send the confirmation prompt. Try again.",
                data={
                    "tool": "memory_forget",
                    "reason": "confirmation_send_failed",
                },
            )

        set_pending_action(
            user,
            disambiguation_id=memory_id,
            action_type="memory_forget",
            arguments={"memory_id": memory_id},
            options=[],
            extra={"memory_id": memory_id},
        )
        await db.flush()

        return ToolResult(
            success=True,
            message="",
            data={
                "tool": "memory_forget",
                "reason": "confirmation_pending",
                "memory_id": memory_id,
                "sent_directly": True,
            },
        )

    async def _send_memory_update_disambiguation(
        self,
        *,
        user: User,
        db: AsyncSession,
        memories: list[Memory],
        query: str,
        new_content: str,
    ) -> ToolResult:
        return await self._send_memory_disambiguation(
            user=user,
            db=db,
            memories=memories,
            action_type="memory_update",
            body_text="Which memory should I update?",
            button_label="Pick memory",
            arguments={"query": query, "new_content": new_content},
        )

    async def _send_memory_forget_disambiguation(
        self,
        *,
        user: User,
        db: AsyncSession,
        memories: list[Memory],
        query: str,
    ) -> ToolResult:
        return await self._send_memory_disambiguation(
            user=user,
            db=db,
            memories=memories,
            action_type="memory_forget_pick",
            body_text="Which memory should I forget?",
            button_label="Pick memory",
            arguments={"query": query},
        )

    async def _send_memory_disambiguation(
        self,
        *,
        user: User,
        db: AsyncSession,
        memories: list[Memory],
        action_type: str,
        body_text: str,
        button_label: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        disambiguation_id = uuid.uuid4().hex
        options_payload = [
            {"memory_id": str(m.id), "snippet": _summarize(m.content)}
            for m in memories
        ]
        client = self._whatsapp_client_factory()

        try:
            if len(memories) <= 3:
                buttons = [
                    {
                        "type": "reply",
                        "reply": {
                            "id": build_option_id(disambiguation_id, idx),
                            "title": _truncate(_summarize(m.content, 18), 20),
                        },
                    }
                    for idx, m in enumerate(memories)
                ]
                await client.send_interactive_buttons(
                    to=user.wa_id, body_text=body_text, buttons=buttons
                )
            else:
                rows = [
                    {
                        "id": build_option_id(disambiguation_id, idx),
                        "title": _truncate(_summarize(m.content, 22), 24),
                        "description": "",
                    }
                    for idx, m in enumerate(memories)
                ]
                await client.send_interactive_list(
                    to=user.wa_id,
                    body_text=body_text,
                    button_text=button_label,
                    sections=[{"title": "Memories", "rows": rows}],
                )
        except Exception:
            logger.exception(
                "Failed to send memory disambiguation for user=%s", user.id
            )
            return ToolResult(
                success=False,
                message=(
                    "I found a few matching memories. Try again with more detail."
                ),
                data={
                    "tool": action_type,
                    "reason": "disambiguation_send_failed",
                },
            )

        set_pending_action(
            user,
            disambiguation_id=disambiguation_id,
            action_type=action_type,
            arguments=arguments,
            options=options_payload,
        )
        await db.flush()

        return ToolResult(
            success=True,
            message="",
            data={
                "tool": action_type,
                "reason": "disambiguation",
                "disambiguation_id": disambiguation_id,
                "options_count": len(memories),
                "sent_directly": True,
            },
        )

    # --- internal helpers --------------------------------------------------

    async def _resolve_event(
        self,
        *,
        arguments: dict[str, Any],
        user: User,
        db: AsyncSession,
        account: GoogleAccount,
        token: str,
    ) -> ResolvedEvent | list[ResolvedEvent] | ToolResult | None:
        search_title = arguments.get("search_title")
        search_date = arguments.get("search_date")

        # Context-aware resolution for vague references like "it" or "that".
        if is_vague_title(search_title):
            recent_messages = await _load_recent_messages(user.id, db)
            event_id = await EventResolver.resolve_from_context(
                recent_messages, search_title
            )
            if event_id:
                fetched = await self._call_with_refresh(
                    user=user,
                    db=db,
                    account=account,
                    token=token,
                    action=lambda access_token: self._calendar_service_factory(
                        access_token, account.calendar_id
                    ).get_event(event_id),
                )
                if isinstance(fetched, ToolResult):
                    return fetched
                resolved = _event_to_resolved(fetched, account.calendar_id)
                if resolved is not None:
                    return resolved
                # Fall through to title-based search if the context event
                # has gone away.

        async def search(access_token: str):
            calendar = self._calendar_service_factory(
                access_token, account.calendar_id
            )

            async def list_events(time_min, time_max, query):
                return await calendar.list_events(
                    time_min=time_min, time_max=time_max, query=query
                )

            resolver = EventResolver(list_events, account.calendar_id)
            return await resolver.resolve(
                search_title=search_title if isinstance(search_title, str) else None,
                search_date=search_date if isinstance(search_date, str) else None,
                user_id=user.id,
                db=db,
            )

        return await self._call_with_refresh(
            user=user, db=db, account=account, token=token, action=search
        )

    async def _apply_calendar_update(
        self,
        *,
        event: ResolvedEvent,
        arguments: dict[str, Any],
        user: User,
        db: AsyncSession,
        account: GoogleAccount,
        token: str,
    ) -> ToolResult:
        timezone = user.timezone or get_settings().default_timezone
        new_title = arguments.get("new_title")
        new_start = arguments.get("new_start")
        new_end = arguments.get("new_end")
        new_description = arguments.get("new_description")

        updates: dict[str, Any] = {}
        if isinstance(new_title, str) and new_title:
            updates["summary"] = new_title
        if isinstance(new_start, str) and new_start:
            updates["start"] = {"dateTime": new_start, "timeZone": timezone}
        if isinstance(new_end, str) and new_end:
            updates["end"] = {"dateTime": new_end, "timeZone": timezone}
        if isinstance(new_description, str):
            updates["description"] = new_description

        if not updates:
            return ToolResult(
                success=False,
                message="What should I change about it?",
                data={"tool": "calendar_update", "reason": "no_changes"},
            )

        result = await self._call_with_refresh(
            user=user,
            db=db,
            account=account,
            token=token,
            action=lambda access_token: self._calendar_service_factory(
                access_token, account.calendar_id
            ).update_event(event.event_id, updates),
        )
        if isinstance(result, ToolResult):
            return result

        final_title = (
            new_title if isinstance(new_title, str) and new_title else event.title
        )
        final_start = (
            new_start if isinstance(new_start, str) and new_start else event.start
        )
        final_end = (
            new_end if isinstance(new_end, str) and new_end else event.end
        )

        parts: list[str] = []
        if isinstance(new_title, str) and new_title:
            parts.append(f"title '{new_title}'")
        if (isinstance(new_start, str) and new_start) or (
            isinstance(new_end, str) and new_end
        ):
            try:
                parts.append(format_event_time(final_start, final_end, timezone))
            except InvalidDateTimeError:
                pass
        if isinstance(new_description, str):
            parts.append("description updated")
        changes_summary = ", ".join(parts) or "details updated"

        return ToolResult(
            success=True,
            message=f"Updated: {final_title} -> {changes_summary}",
            data={
                "tool": "calendar_update",
                "google_event_id": event.event_id,
                "calendar_id": event.calendar_id,
                "title": final_title,
                "start": final_start,
                "end": final_end,
            },
        )

    async def _apply_calendar_cancel(
        self,
        *,
        event: ResolvedEvent,
        user: User,
        db: AsyncSession,
        account: GoogleAccount,
        token: str,
    ) -> ToolResult:
        timezone = user.timezone or get_settings().default_timezone

        result = await self._call_with_refresh(
            user=user,
            db=db,
            account=account,
            token=token,
            action=lambda access_token: self._calendar_service_factory(
                access_token, account.calendar_id
            ).delete_event(event.event_id),
        )
        if isinstance(result, ToolResult):
            return result

        await _delete_event_reference(user.id, event.event_id, db)

        try:
            formatted = format_event_time(event.start, event.end, timezone)
        except InvalidDateTimeError:
            formatted = ""
        suffix = f" - {formatted}" if formatted else ""
        return ToolResult(
            success=True,
            message=f"Cancelled: {event.title}{suffix}",
            data={
                "tool": "calendar_cancel",
                "google_event_id": event.event_id,
                "calendar_id": event.calendar_id,
                "title": event.title,
            },
        )

    async def _send_disambiguation(
        self,
        *,
        action_type: str,
        action_label: str,
        arguments: dict[str, Any],
        user: User,
        db: AsyncSession,
        events: list[ResolvedEvent],
    ) -> ToolResult:
        timezone = user.timezone or get_settings().default_timezone
        client = self._whatsapp_client_factory()
        service = self._disambiguation_service_factory(client)
        try:
            disambiguation_id = await service.send_event_choices(
                wa_id=user.wa_id,
                events=events,
                action=action_label,
                timezone_name=timezone,
            )
        except Exception:
            logger.exception(
                "Failed to send disambiguation prompt for user=%s", user.id
            )
            return ToolResult(
                success=False,
                message="I found a few matching events. Try again with more detail.",
                data={"tool": action_type, "reason": "disambiguation_send_failed"},
            )

        set_pending_action(
            user,
            disambiguation_id=disambiguation_id,
            action_type=action_type,
            arguments=arguments,
            options=events,
        )
        await db.flush()

        return ToolResult(
            success=True,
            message="",
            data={
                "tool": action_type,
                "reason": "disambiguation",
                "disambiguation_id": disambiguation_id,
                "options_count": len(events),
                "sent_directly": True,
            },
        )

    async def _get_token_and_account(
        self, user: User, db: AsyncSession
    ) -> tuple[str | None, GoogleAccount | None]:
        auth = self._google_auth_factory(db)
        token = await auth.get_valid_token(user.id)
        if token is None:
            return None, None
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


async def execute_pending_action(
    *,
    pending: dict[str, Any],
    selected_event: ResolvedEvent,
    user: User,
    db: AsyncSession,
    inbound_message_id: uuid.UUID | None = None,
    tool_executor: ToolExecutor,
) -> ToolResult:
    """Replay a previously parked tool call against the user-selected event."""
    action_type = pending.get("type")
    arguments = pending.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    if action_type not in {"calendar_update", "calendar_cancel"}:
        return ToolResult(
            success=False,
            message="That selection has expired.",
            data={"reason": "unknown_pending_type"},
        )
    clear_pending_action(user)
    return await tool_executor.execute(
        action_type,
        arguments,
        user=user,
        db=db,
        inbound_message_id=inbound_message_id,
        preselected_event=selected_event,
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


def _event_to_resolved(
    event: dict[str, Any], calendar_id: str
) -> ResolvedEvent | None:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    summary = str(event.get("summary") or "(no title)")
    start_obj = event.get("start") or {}
    end_obj = event.get("end") or {}
    start = start_obj.get("dateTime") or start_obj.get("date")
    end = end_obj.get("dateTime") or end_obj.get("date")
    if not start or not end:
        return None
    return ResolvedEvent(
        event_id=event_id,
        title=summary,
        start=str(start),
        end=str(end),
        calendar_id=calendar_id,
    )


def _not_found_result(tool: str, arguments: dict[str, Any]) -> ToolResult:
    title = arguments.get("search_title")
    if isinstance(title, str) and title.strip():
        message = f"I couldn't find an event matching '{title.strip()}'."
    else:
        message = "I couldn't find that event."
    return ToolResult(
        success=False,
        message=message,
        data={"tool": tool, "reason": "not_found"},
    )


async def _load_recent_messages(
    user_id: uuid.UUID, db: AsyncSession, limit: int = 10
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())


def _summarize(content: str, length: int = 50) -> str:
    """First ``length`` chars of ``content`` plus an ellipsis when truncated."""
    text = content.strip()
    if len(text) <= length:
        return text
    return text[:length] + "..."


def _truncate(value: str, max_len: int) -> str:
    """Truncate while reserving 3 chars for the ellipsis when possible."""
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return value[: max_len - 3] + "..."


async def _delete_event_reference(
    user_id: uuid.UUID, google_event_id: str, db: AsyncSession
) -> None:
    result = await db.execute(
        select(EventReference).where(
            EventReference.user_id == user_id,
            EventReference.google_event_id == google_event_id,
        )
    )
    for row in result.scalars().all():
        await db.delete(row)
    await db.flush()
