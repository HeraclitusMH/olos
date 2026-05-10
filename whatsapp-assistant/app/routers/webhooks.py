"""WhatsApp webhook endpoints (Meta verification + inbound messages)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_settings
from app.services.message_processor import InboundMessage, process_inbound_message
from app.utils.signature import validate_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks")

# Set of message types we process end-to-end. Other types are acknowledged
# but not echoed.
_SUPPORTED_MESSAGE_TYPES = {"text", "interactive"}


@router.get("/whatsapp")
async def verify_whatsapp(request: Request) -> Response:
    """Handle Meta's webhook verification challenge.

    Meta calls GET with ``hub.mode``, ``hub.verify_token``, ``hub.challenge``.
    Echo the challenge back as plain text on success, 403 otherwise.
    """
    settings = get_settings()
    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("WhatsApp webhook verification succeeded")
        return PlainTextResponse(content=challenge or "", status_code=200)

    logger.warning(
        "WhatsApp webhook verification failed: mode=%s token_match=%s",
        mode,
        token == settings.whatsapp_verify_token,
    )
    return PlainTextResponse(content="Forbidden", status_code=403)


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> JSONResponse:
    """Receive an incoming WhatsApp webhook from Meta.

    Validates the request signature, parses the payload, schedules
    background processing for any text messages, and returns 200
    immediately so Meta does not retry.
    """
    settings = get_settings()
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    if not validate_webhook_signature(
        raw_body, signature_header, settings.meta_app_secret
    ):
        logger.warning(
            "Invalid webhook signature; refusing to process payload (returning 200)"
        )
        return JSONResponse({"status": "received"}, status_code=200)

    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.exception("Failed to decode webhook JSON body")
        return JSONResponse({"status": "received"}, status_code=200)

    try:
        inbound_messages = _parse_inbound_messages(payload)
    except Exception:
        logger.exception("Failed to parse webhook payload")
        return JSONResponse({"status": "received"}, status_code=200)

    for inbound in inbound_messages:
        # Fire-and-forget: never block the 200 response on processing.
        asyncio.create_task(process_inbound_message(inbound))

    return JSONResponse({"status": "received"}, status_code=200)


def _parse_inbound_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Walk the Meta webhook envelope and return inbound text messages.

    Silently ignores delivery-status webhooks and non-text message types.
    """
    results: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}

            # Status webhooks (delivered/read receipts) carry no "messages".
            if "statuses" in value and "messages" not in value:
                continue

            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id")

            for message in value.get("messages") or []:
                msg_type = message.get("type")
                wa_message_id = message.get("id")
                wa_id = message.get("from")

                if not wa_message_id or not wa_id:
                    logger.warning(
                        "Skipping message missing id or from: %s", message
                    )
                    continue

                if msg_type not in _SUPPORTED_MESSAGE_TYPES:
                    logger.info(
                        "Acknowledging unsupported message type=%s id=%s",
                        msg_type,
                        wa_message_id,
                    )
                    continue

                if msg_type == "interactive":
                    parsed = _parse_interactive(message)
                    if parsed is None:
                        logger.info(
                            "Acknowledging unsupported interactive payload id=%s",
                            wa_message_id,
                        )
                        continue
                    interactive_id, reply_text = parsed
                    results.append(
                        InboundMessage(
                            wa_id=wa_id,
                            wa_message_id=wa_message_id,
                            text=reply_text,
                            phone_number_id=phone_number_id,
                            interactive_id=interactive_id,
                        )
                    )
                    continue

                text_body = ((message.get("text") or {}).get("body")) or ""
                results.append(
                    InboundMessage(
                        wa_id=wa_id,
                        wa_message_id=wa_message_id,
                        text=text_body,
                        phone_number_id=phone_number_id,
                    )
                )
    return results


def _parse_interactive(message: dict[str, Any]) -> tuple[str, str] | None:
    interactive = message.get("interactive") or {}
    itype = interactive.get("type")
    if itype == "button_reply":
        reply = interactive.get("button_reply") or {}
    elif itype == "list_reply":
        reply = interactive.get("list_reply") or {}
    else:
        return None
    reply_id = reply.get("id")
    if not isinstance(reply_id, str) or not reply_id:
        return None
    title = reply.get("title")
    return reply_id, title if isinstance(title, str) else ""
