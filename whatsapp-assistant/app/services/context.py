"""Conversation context assembly for OpenAI planning."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message


async def build_conversation_context(
    user_id: uuid.UUID,
    db: AsyncSession,
    limit: int = 20,
) -> list[dict[str, str]]:
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id, Message.content.is_not(None))
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    messages = list(reversed(result.scalars().all()))

    context: list[dict[str, str]] = []
    for message in messages:
        role = _role_for_direction(message.direction)
        if role is None or message.content is None:
            continue
        context.append({"role": role, "content": message.content})
    return context


def _role_for_direction(direction: str) -> str | None:
    if direction == "inbound":
        return "user"
    if direction == "outbound":
        return "assistant"
    return None

