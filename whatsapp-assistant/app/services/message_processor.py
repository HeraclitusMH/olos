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
from app.services.planner import Planner
from app.services.tool_executor import ToolExecutionLog, ToolExecutor, ToolResult
from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound WhatsApp message ready for processing."""

    wa_id: str
    wa_message_id: str
    text: str
    phone_number_id: str | None = None


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
    """Process one inbound text message.

    Steps: find/create user, dedupe by wa_message_id, store inbound message,
    ask the LLM planner for tool calls, execute each tool call via the
    ``ToolExecutor``, store outbound message, and mark inbound processed.
    All DB work runs in a single async session/transaction.
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
            conversation_history = await build_conversation_context(
                user.id, session, limit=20
            )

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

            if not await cost_tracker.check_limit(session):
                execution.replies.append("Daily limit reached. Try again tomorrow!")
                tool_calls: list[dict[str, Any]] = []
            else:
                await cost_tracker.increment(session)
                tool_calls = await planner.plan(
                    inbound.text,
                    conversation_history,
                    user.timezone or get_settings().default_timezone,
                )
                inbound_msg.tool_calls_json = {"tool_calls": tool_calls}
                execution = await _execute_tool_calls(
                    tool_calls,
                    user=user,
                    session=session,
                    inbound_message_id=inbound_msg.id,
                    tool_executor=tool_executor,
                )

            reply_text = execution.combined_reply

            send_error: str | None = None
            api_response: dict | None = None
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
    return log


def _extract_outbound_message_id(api_response: dict | None) -> str | None:
    if not api_response:
        return None
    messages = api_response.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")
