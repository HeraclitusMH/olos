"""Background processing for inbound WhatsApp messages."""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    execute_pending_action,
)
from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient

logger = logging.getLogger(__name__)


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


async def process_inbound_message(
    inbound: InboundMessage,
    *,
    whatsapp_client: WhatsAppClient | None = None,
    planner: Planner | None = None,
    cost_tracker: CostTracker | None = None,
    tool_executor: ToolExecutor | None = None,
) -> None:
    """Process one inbound WhatsApp message (text or interactive reply).

    Steps: find/create user, dedupe by wa_message_id, store inbound message,
    branch on whether it is an interactive reply (resume the parked
    pending action) or a text message (plan + execute via the LLM
    planner). Send the outbound reply if there is one to send.
    """
    client = whatsapp_client or WhatsAppClient()
    planner = planner or Planner()
    cost_tracker = cost_tracker or CostTracker()
    tool_executor = tool_executor or ToolExecutor()

    try:
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

            reply_text = execution.combined_reply
            send_error: str | None = None
            api_response: dict | None = None
            if reply_text:
                try:
                    api_response = await client.send_text_message(
                        to=inbound.wa_id, text=reply_text
                    )
                except (WhatsAppAPIError, httpx.HTTPError) as exc:
                    logger.exception(
                        "Failed to send reply to wa_id=%s: %s", inbound.wa_id, exc
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

            await session.commit()
            logger.info(
                "Processed inbound message wa_message_id=%s user_id=%s",
                inbound.wa_message_id,
                user.id,
            )
    except Exception:
        logger.critical(
            "Unhandled error processing inbound wa_message_id=%s",
            inbound.wa_message_id,
            exc_info=True,
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
        execution.replies.append("Daily limit reached. Try again tomorrow!")
        return execution

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
) -> ToolExecutionLog:
    log = ToolExecutionLog()
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
    selected_event = (
        deserialize_event(option_payload) if isinstance(option_payload, dict) else None
    )
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
