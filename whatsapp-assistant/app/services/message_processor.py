"""Background processing for inbound WhatsApp messages."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import Message, User
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
) -> None:
    """Process one inbound text message.

    Steps: find/create user, dedupe by wa_message_id, store inbound message,
    echo back via WhatsApp Cloud API, store outbound message, mark inbound
    processed. All DB work runs in a single async session/transaction.
    """
    client = whatsapp_client or WhatsAppClient()

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

            echo_text = f"Echo: {inbound.text}"
            send_error: str | None = None
            api_response: dict | None = None
            try:
                api_response = await client.send_text_message(
                    to=inbound.wa_id, text=echo_text
                )
            except (WhatsAppAPIError, httpx.HTTPError) as exc:
                logger.exception(
                    "Failed to send echo to wa_id=%s: %s", inbound.wa_id, exc
                )
                send_error = str(exc)

            outbound_wa_id = _extract_outbound_message_id(api_response)
            outbound_msg = Message(
                user_id=user.id,
                wa_message_id=outbound_wa_id,
                direction="outbound",
                content=echo_text,
                processed=True,
                error=send_error,
            )
            session.add(outbound_msg)

            inbound_msg.processed = True

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


def _extract_outbound_message_id(api_response: dict | None) -> str | None:
    if not api_response:
        return None
    messages = api_response.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")
