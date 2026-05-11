"""Background processing for inbound WhatsApp messages."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import Message, User
from app.services.context import build_conversation_context
from app.services.cost_tracker import CostTracker
from app.services.disambiguation import parse_option_id
from app.services.google_auth import GoogleAuthError, build_authorization_url
from app.services.memory import MemoryService
from app.services.pending_action import (
    clear_pending_action,
    deserialize_event,
    get_active_pending_action,
)
from app.services.planner import Planner
from app.services.tool_executor import (
    ToolExecutionLog,
    ToolExecutor,
    ToolResult,
    _summarize,
    execute_pending_action,
)
from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.utils.exceptions import (
    AssistantError,
    DailyLimitExceededError,
    GoogleCalendarError,
    OpenAIError,
    TokenExpiredError,
    WhatsAppSendError,
)
from app.utils.search import generate_tags

logger = logging.getLogger(__name__)

# Hard upper bound on per-message work; beyond this we surface a timeout reply.
PROCESSING_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound WhatsApp message ready for processing."""

    wa_id: str
    wa_message_id: str
    text: str
    phone_number_id: str | None = None
    interactive_id: str | None = None


async def _get_or_create_user(session: AsyncSession, wa_id: str) -> User:
    result = await session.execute(select(User).where(User.wa_id == wa_id))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    user = User(wa_id=wa_id)
    session.add(user)
    await session.flush()
    logger.info("Created new user wa_id=%s id=%s", wa_id, user.id)
    return user


async def _is_duplicate(session: AsyncSession, wa_message_id: str) -> bool:
    result = await session.execute(
        select(Message.id).where(Message.wa_message_id == wa_message_id)
    )
    return result.scalar_one_or_none() is not None


def _error_reply(exc: BaseException, user: User) -> str:
    """Translate an AssistantError into a short WhatsApp-safe reply."""
    if isinstance(exc, DailyLimitExceededError):
        return "Hit my daily limit. Back tomorrow!"
    if isinstance(exc, OpenAIError):
        return "Having trouble thinking right now. Try again in a minute."
    if isinstance(exc, TokenExpiredError):
        url = build_authorization_url(user.wa_id)
        return (
            "Your Google connection expired. Please re-authorize: "
            f"{url}"
        )
    if isinstance(exc, GoogleCalendarError):
        return "Calendar temporarily unavailable."
    return "Something went wrong."


async def process_inbound_message(
    inbound: InboundMessage,
    *,
    whatsapp_client: WhatsAppClient | None = None,
    planner: Planner | None = None,
    cost_tracker: CostTracker | None = None,
    tool_executor: ToolExecutor | None = None,
) -> None:
    """Process one inbound WhatsApp message (text or interactive reply).

    Wraps the entire flow in a 25-second timeout and an outer try/except so a
    failure is *always* reported back to the user and the inbound row is
    *always* marked processed (the golden rule: no silent drops).
    """
    client = whatsapp_client or WhatsAppClient()
    planner = planner or Planner()
    cost_tracker = cost_tracker or CostTracker()
    tool_executor = tool_executor or ToolExecutor()
    started = time.monotonic()

    logger.info(
        "Inbound message received",
        extra={
            "event": "inbound_message",
            "wa_message_id": inbound.wa_message_id,
            "wa_id": inbound.wa_id,
            "is_interactive": inbound.interactive_id is not None,
        },
    )

    try:
        await asyncio.wait_for(
            _do_process(
                inbound,
                client=client,
                planner=planner,
                cost_tracker=cost_tracker,
                tool_executor=tool_executor,
                started=started,
            ),
            timeout=PROCESSING_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.error(
            "Processing exceeded %ds for wa_message_id=%s",
            PROCESSING_TIMEOUT_SECONDS,
            inbound.wa_message_id,
            extra={
                "event": "processing_timeout",
                "wa_message_id": inbound.wa_message_id,
            },
        )
        await _finalize_failure(
            inbound,
            client=client,
            reply_text="That took too long. Please try again.",
            error_label="processing_timeout",
        )
    except Exception:  # noqa: BLE001
        # Last-resort guard: _do_process already catches everything, but if the
        # session/commit step itself blows up we still need to mark the row.
        logger.critical(
            "Unhandled error processing inbound wa_message_id=%s",
            inbound.wa_message_id,
            exc_info=True,
            extra={
                "event": "processing_unhandled",
                "wa_message_id": inbound.wa_message_id,
            },
        )
        await _finalize_failure(
            inbound,
            client=client,
            reply_text="Something went wrong.",
            error_label="unhandled_exception",
        )


async def _do_process(
    inbound: InboundMessage,
    *,
    client: WhatsAppClient,
    planner: Planner,
    cost_tracker: CostTracker,
    tool_executor: ToolExecutor,
    started: float,
) -> None:
    async with AsyncSessionLocal() as session:
        if await _is_duplicate(session, inbound.wa_message_id):
            logger.info(
                "Duplicate inbound wa_message_id=%s — skipping",
                inbound.wa_message_id,
            )
            return

        user = await _get_or_create_user(session, inbound.wa_id)

        inbound_msg = Message(
            user_id=user.id,
            wa_message_id=inbound.wa_message_id,
            direction="inbound",
            content=inbound.text,
            processed=False,
        )
        session.add(inbound_msg)
        await session.flush()

        execution = ToolExecutionLog()
        error_label: str | None = None
        try:
            if inbound.interactive_id:
                execution = await _process_interactive_reply(
                    interactive_id=inbound.interactive_id,
                    user=user,
                    session=session,
                    inbound_message_id=inbound_msg.id,
                    tool_executor=tool_executor,
                )
            else:
                execution = await _process_text_message(
                    inbound=inbound,
                    user=user,
                    session=session,
                    inbound_message_id=inbound_msg.id,
                    inbound_msg=inbound_msg,
                    cost_tracker=cost_tracker,
                    planner=planner,
                    tool_executor=tool_executor,
                )
        except DailyLimitExceededError as exc:
            error_label = "daily_limit_exceeded"
            execution = ToolExecutionLog(replies=[_error_reply(exc, user)])
        except OpenAIError as exc:
            error_label = "openai_error"
            logger.warning(
                "OpenAI error while processing message: %s",
                exc.log_message,
                extra={"event": "openai_error", "wa_message_id": inbound.wa_message_id},
            )
            execution = ToolExecutionLog(replies=[_error_reply(exc, user)])
        except TokenExpiredError as exc:
            error_label = "token_expired"
            logger.warning(
                "Token expired: %s",
                exc.log_message,
                extra={"event": "token_expired", "wa_message_id": inbound.wa_message_id},
            )
            execution = ToolExecutionLog(replies=[_error_reply(exc, user)])
        except GoogleAuthError as exc:
            error_label = "google_auth_transient"
            logger.warning(
                "Transient Google auth failure: %s",
                exc,
                extra={
                    "event": "google_auth_transient",
                    "wa_message_id": inbound.wa_message_id,
                },
            )
            execution = ToolExecutionLog(
                replies=["Calendar temporarily unavailable."]
            )
        except GoogleCalendarError as exc:
            error_label = "google_calendar_error"
            logger.warning(
                "Google Calendar error: %s",
                exc.log_message,
                extra={
                    "event": "google_calendar_error",
                    "wa_message_id": inbound.wa_message_id,
                },
            )
            execution = ToolExecutionLog(replies=[_error_reply(exc, user)])
        except AssistantError as exc:
            error_label = "assistant_error"
            logger.error(
                "Unhandled assistant error: %s",
                exc.log_message,
                exc_info=True,
                extra={
                    "event": "assistant_error",
                    "wa_message_id": inbound.wa_message_id,
                },
            )
            execution = ToolExecutionLog(replies=[_error_reply(exc, user)])
        except Exception:  # noqa: BLE001
            error_label = "unhandled_exception"
            logger.critical(
                "Unexpected error processing message wa_message_id=%s",
                inbound.wa_message_id,
                exc_info=True,
                extra={
                    "event": "unhandled_exception",
                    "wa_message_id": inbound.wa_message_id,
                },
            )
            execution = ToolExecutionLog(replies=["Something went wrong."])

        reply_text = execution.combined_reply
        send_error: str | None = None
        api_response: dict | None = None
        if reply_text:
            try:
                api_response = await client.send_text_message(
                    to=inbound.wa_id, text=reply_text
                )
            except (WhatsAppSendError, WhatsAppAPIError, httpx.HTTPError) as exc:
                logger.exception(
                    "Failed to send reply to wa_id=%s: %s",
                    inbound.wa_id,
                    exc,
                    extra={
                        "event": "whatsapp_send_failed",
                        "wa_message_id": inbound.wa_message_id,
                    },
                )
                send_error = str(exc)

        outbound_wa_id = _extract_outbound_message_id(api_response)
        outbound_msg = Message(
            user_id=user.id,
            wa_message_id=outbound_wa_id,
            direction="outbound",
            content=reply_text,
            processed=True,
            error=send_error,
        )
        if execution.results:
            outbound_msg.execution_result_json = {"results": execution.results}
        session.add(outbound_msg)

        inbound_msg.processed = True
        if execution.results:
            inbound_msg.execution_result_json = {"results": execution.results}
        if error_label is not None:
            inbound_msg.error = error_label
        duration_ms = int((time.monotonic() - started) * 1000)
        inbound_msg.processing_duration_ms = duration_ms

        await session.commit()
        logger.info(
            "Processed inbound message",
            extra={
                "event": "processed",
                "wa_message_id": inbound.wa_message_id,
                "user_id": str(user.id),
                "duration_ms": duration_ms,
                "error": error_label,
            },
        )


async def _finalize_failure(
    inbound: InboundMessage,
    *,
    client: WhatsAppClient,
    reply_text: str,
    error_label: str,
) -> None:
    """Mark the inbound row processed and try to notify the user.

    Called when the normal session lifecycle inside ``_do_process`` could not
    complete (timeout, unhandled exception). Opens a fresh session so we never
    leave a row sitting with ``processed=False`` after a hard failure.
    """
    try:
        async with AsyncSessionLocal() as session:
            row = await session.execute(
                select(Message).where(
                    Message.wa_message_id == inbound.wa_message_id,
                    Message.direction == "inbound",
                )
            )
            inbound_msg = row.scalar_one_or_none()
            if inbound_msg is None:
                # The row never made it in — create a stub so we don't lose the
                # message in the audit trail.
                user = await _get_or_create_user(session, inbound.wa_id)
                inbound_msg = Message(
                    user_id=user.id,
                    wa_message_id=inbound.wa_message_id,
                    direction="inbound",
                    content=inbound.text,
                    processed=True,
                    error=error_label,
                )
                session.add(inbound_msg)
            else:
                inbound_msg.processed = True
                inbound_msg.error = error_label
            await session.commit()
    except Exception:  # noqa: BLE001 - last resort
        logger.critical(
            "Failed to finalize inbound row after failure for wa_message_id=%s",
            inbound.wa_message_id,
            exc_info=True,
        )

    try:
        await client.send_text_message(to=inbound.wa_id, text=reply_text)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not send fallback reply to wa_id=%s", inbound.wa_id
        )


async def _process_text_message(
    *,
    inbound: InboundMessage,
    user: User,
    session: AsyncSession,
    inbound_message_id: Any,
    inbound_msg: Message,
    cost_tracker: CostTracker,
    planner: Planner,
    tool_executor: ToolExecutor,
) -> ToolExecutionLog:
    conversation_history = await build_conversation_context(
        user.id, session, limit=20
    )
    execution = ToolExecutionLog()

    if not await cost_tracker.check_limit(session):
        raise DailyLimitExceededError("daily OpenAI request budget reached")

    await cost_tracker.increment(session)
    tool_calls = await planner.plan(
        inbound.text,
        conversation_history,
        user.timezone or get_settings().default_timezone,
    )
    inbound_msg.tool_calls_json = {"tool_calls": tool_calls}
    return await _execute_tool_calls(
        tool_calls,
        user=user,
        session=session,
        inbound_message_id=inbound_message_id,
        tool_executor=tool_executor,
    )


async def _process_interactive_reply(
    *,
    interactive_id: str,
    user: User,
    session: AsyncSession,
    inbound_message_id: Any,
    tool_executor: ToolExecutor,
    memory_service: MemoryService | None = None,
) -> ToolExecutionLog:
    log = ToolExecutionLog()
    memory_service = memory_service or MemoryService()

    if interactive_id.startswith("confirm_forget_") or interactive_id.startswith(
        "cancel_forget_"
    ):
        return await _process_forget_confirmation(
            interactive_id=interactive_id,
            user=user,
            session=session,
            memory_service=memory_service,
        )

    parsed = parse_option_id(interactive_id)
    if parsed is None:
        log.replies.append("I don't have a pending request for that selection.")
        return log
    disambig_id, option_index = parsed

    pending = get_active_pending_action(user)
    if pending is None or pending.get("id") != disambig_id:
        clear_pending_action(user)
        log.replies.append("That selection has expired. Please ask again.")
        log.results.append(
            {
                "tool": "disambiguation",
                "success": False,
                "data": {"reason": "expired_or_missing"},
            }
        )
        return log

    options = pending.get("options") or []
    if not isinstance(options, list) or option_index < 0 or option_index >= len(
        options
    ):
        clear_pending_action(user)
        log.replies.append("That selection is no longer valid.")
        log.results.append(
            {
                "tool": "disambiguation",
                "success": False,
                "data": {"reason": "invalid_option"},
            }
        )
        return log

    option_payload = options[option_index]
    if not isinstance(option_payload, dict):
        clear_pending_action(user)
        log.replies.append("That selection is no longer valid.")
        log.results.append(
            {
                "tool": "disambiguation",
                "success": False,
                "data": {"reason": "invalid_option"},
            }
        )
        return log

    action_type = pending.get("type")

    if action_type in {"calendar_update", "calendar_cancel"}:
        return await _resume_calendar_disambiguation(
            log=log,
            pending=pending,
            option_payload=option_payload,
            disambig_id=disambig_id,
            user=user,
            session=session,
            inbound_message_id=inbound_message_id,
            tool_executor=tool_executor,
        )

    if action_type == "memory_update":
        return await _resume_memory_update(
            log=log,
            pending=pending,
            option_payload=option_payload,
            disambig_id=disambig_id,
            user=user,
            session=session,
            memory_service=memory_service,
        )

    if action_type == "memory_forget_pick":
        return await _resume_memory_forget_pick(
            log=log,
            option_payload=option_payload,
            disambig_id=disambig_id,
            user=user,
            session=session,
            tool_executor=tool_executor,
            memory_service=memory_service,
        )

    clear_pending_action(user)
    log.replies.append("That selection has expired. Please ask again.")
    log.results.append(
        {
            "tool": "disambiguation",
            "success": False,
            "data": {"reason": "unknown_pending_type"},
        }
    )
    return log


async def _resume_calendar_disambiguation(
    *,
    log: ToolExecutionLog,
    pending: dict[str, Any],
    option_payload: dict[str, Any],
    disambig_id: str,
    user: User,
    session: AsyncSession,
    inbound_message_id: Any,
    tool_executor: ToolExecutor,
) -> ToolExecutionLog:
    selected_event = deserialize_event(option_payload)
    if selected_event is None:
        clear_pending_action(user)
        log.replies.append("That selection is no longer valid.")
        log.results.append(
            {
                "tool": "disambiguation",
                "success": False,
                "data": {"reason": "deserialize_failed"},
            }
        )
        return log

    result: ToolResult = await execute_pending_action(
        pending=pending,
        selected_event=selected_event,
        user=user,
        db=session,
        inbound_message_id=inbound_message_id,
        tool_executor=tool_executor,
    )
    if result.message:
        log.replies.append(result.message)
    log.results.append(
        {
            "tool": pending.get("type") or "disambiguation",
            "success": result.success,
            "data": result.data,
            "disambiguation_id": disambig_id,
            "selected_event_id": selected_event.event_id,
        }
    )
    if (result.data or {}).get("sent_directly"):
        log.sent_directly = True
    return log


async def _resume_memory_update(
    *,
    log: ToolExecutionLog,
    pending: dict[str, Any],
    option_payload: dict[str, Any],
    disambig_id: str,
    user: User,
    session: AsyncSession,
    memory_service: MemoryService,
) -> ToolExecutionLog:
    raw_id = option_payload.get("memory_id")
    arguments = pending.get("arguments") or {}
    new_content = arguments.get("new_content")
    memory_uuid = _safe_uuid(raw_id)
    if memory_uuid is None or not isinstance(new_content, str) or not new_content.strip():
        clear_pending_action(user)
        log.replies.append("That selection is no longer valid.")
        log.results.append(
            {
                "tool": "memory_update",
                "success": False,
                "data": {"reason": "invalid_option"},
            }
        )
        return log

    memory = await memory_service.get_by_id(memory_uuid, user.id, session)
    if memory is None:
        clear_pending_action(user)
        log.replies.append("That memory is no longer available.")
        log.results.append(
            {
                "tool": "memory_update",
                "success": False,
                "data": {"reason": "memory_missing"},
            }
        )
        return log

    new_text = new_content.strip()
    old_summary = _summarize(memory.content)
    new_tags = await generate_tags(new_text)
    await memory_service.update_content(
        memory=memory, new_content=new_text, new_tags=new_tags, db=session
    )
    clear_pending_action(user)
    log.replies.append(f"Updated: {old_summary} -> {_summarize(new_text)}")
    log.results.append(
        {
            "tool": "memory_update",
            "success": True,
            "data": {"memory_id": str(memory.id)},
            "disambiguation_id": disambig_id,
        }
    )
    return log


async def _resume_memory_forget_pick(
    *,
    log: ToolExecutionLog,
    option_payload: dict[str, Any],
    disambig_id: str,
    user: User,
    session: AsyncSession,
    tool_executor: ToolExecutor,
    memory_service: MemoryService,
) -> ToolExecutionLog:
    raw_id = option_payload.get("memory_id")
    memory_uuid = _safe_uuid(raw_id)
    if memory_uuid is None:
        clear_pending_action(user)
        log.replies.append("That selection is no longer valid.")
        log.results.append(
            {
                "tool": "memory_forget",
                "success": False,
                "data": {"reason": "invalid_option"},
            }
        )
        return log

    memory = await memory_service.get_by_id(memory_uuid, user.id, session)
    if memory is None:
        clear_pending_action(user)
        log.replies.append("That memory is no longer available.")
        log.results.append(
            {
                "tool": "memory_forget",
                "success": False,
                "data": {"reason": "memory_missing"},
            }
        )
        return log

    clear_pending_action(user)
    confirmation = await tool_executor._send_forget_confirmation(
        user=user, db=session, memory=memory
    )
    if confirmation.message:
        log.replies.append(confirmation.message)
    log.results.append(
        {
            "tool": "memory_forget",
            "success": confirmation.success,
            "data": confirmation.data,
            "disambiguation_id": disambig_id,
        }
    )
    if (confirmation.data or {}).get("sent_directly"):
        log.sent_directly = True
    return log


async def _process_forget_confirmation(
    *,
    interactive_id: str,
    user: User,
    session: AsyncSession,
    memory_service: MemoryService,
) -> ToolExecutionLog:
    log = ToolExecutionLog()
    if interactive_id.startswith("confirm_forget_"):
        is_confirm = True
        raw_id = interactive_id[len("confirm_forget_"):]
    else:
        is_confirm = False
        raw_id = interactive_id[len("cancel_forget_"):]

    pending = get_active_pending_action(user)
    pending_id = (
        pending.get("memory_id") if isinstance(pending, dict) else None
    )
    pending_type = pending.get("type") if isinstance(pending, dict) else None
    memory_uuid = _safe_uuid(raw_id)

    if (
        pending is None
        or pending_type != "memory_forget"
        or pending_id != raw_id
        or memory_uuid is None
    ):
        clear_pending_action(user)
        log.replies.append("That selection has expired. Please ask again.")
        log.results.append(
            {
                "tool": "memory_forget",
                "success": False,
                "data": {"reason": "expired_or_missing"},
            }
        )
        return log

    if not is_confirm:
        clear_pending_action(user)
        log.replies.append("Okay, keeping it.")
        log.results.append(
            {
                "tool": "memory_forget",
                "success": True,
                "data": {"action": "cancelled", "memory_id": raw_id},
            }
        )
        return log

    memory = await memory_service.get_by_id(memory_uuid, user.id, session)
    if memory is None:
        clear_pending_action(user)
        log.replies.append("That memory is no longer available.")
        log.results.append(
            {
                "tool": "memory_forget",
                "success": False,
                "data": {"reason": "memory_missing"},
            }
        )
        return log

    await memory_service.forget(memory=memory, db=session)
    clear_pending_action(user)
    log.replies.append("Forgotten.")
    log.results.append(
        {
            "tool": "memory_forget",
            "success": True,
            "data": {"action": "forgotten", "memory_id": str(memory.id)},
        }
    )
    return log


def _safe_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    user: User,
    session: AsyncSession,
    inbound_message_id: Any,
    tool_executor: ToolExecutor,
) -> ToolExecutionLog:
    log = ToolExecutionLog()
    for tool_call in tool_calls:
        name = tool_call.get("name")
        arguments = tool_call.get("arguments") or {}
        if not isinstance(name, str):
            continue
        result: ToolResult = await tool_executor.execute(
            name,
            arguments,
            user=user,
            db=session,
            inbound_message_id=inbound_message_id,
        )
        if result.message:
            log.replies.append(result.message)
        log.results.append(
            {
                "tool": name,
                "success": result.success,
                "data": result.data,
            }
        )
        if (result.data or {}).get("sent_directly"):
            log.sent_directly = True
    return log


def _extract_outbound_message_id(api_response: dict | None) -> str | None:
    if not api_response:
        return None
    messages = api_response.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")


async def warn_about_unprocessed_messages() -> None:
    """Log a warning for messages that never finished processing.

    Called once at startup. Does NOT auto-retry — we surface visibility and
    leave recovery decisions to the operator.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=5)
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Message.id, Message.wa_message_id, Message.created_at)
                .where(Message.processed.is_(False))
                .where(Message.created_at < cutoff)
            )
            stuck = result.all()
    except Exception:  # noqa: BLE001
        logger.exception("Could not query unprocessed messages on startup")
        return

    if not stuck:
        return

    logger.warning(
        "Found %d unprocessed messages older than 5 minutes; manual review required",
        len(stuck),
        extra={
            "event": "unprocessed_messages",
            "count": len(stuck),
            "wa_message_ids": [row.wa_message_id for row in stuck],
        },
    )
