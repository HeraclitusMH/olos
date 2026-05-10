from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from app.routers.webhooks import _parse_inbound_messages

VERIFY_TOKEN = "verify-token-test"
APP_SECRET = "meta-secret-test"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _text_message_payload(
    *,
    wa_id: str = "34600111222",
    wa_message_id: str = "wamid.AAAA",
    text: str = "hello",
    phone_number_id: str = "1234567890",
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "12345",
                                "phone_number_id": phone_number_id,
                            },
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": wa_message_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


# --- GET /webhooks/whatsapp (verification) ----------------------------------


async def test_get_verification_returns_challenge_with_correct_token(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1234567890",
        },
    )

    assert response.status_code == 200
    assert response.text == "1234567890"


async def test_get_verification_rejects_wrong_token(client: AsyncClient) -> None:
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "1234567890",
        },
    )

    assert response.status_code == 403


async def test_get_verification_rejects_wrong_mode(client: AsyncClient) -> None:
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1234567890",
        },
    )

    assert response.status_code == 403


# --- POST /webhooks/whatsapp (receive) --------------------------------------


async def test_post_with_valid_signature_schedules_processing(
    client: AsyncClient,
) -> None:
    payload = _text_message_payload()
    body = json.dumps(payload).encode()

    with patch(
        "app.routers.webhooks.process_inbound_message",
        new=AsyncMock(return_value=None),
    ) as mock_process:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    # Background task should have been scheduled and awaited by the loop.
    mock_process.assert_called_once()
    inbound = mock_process.call_args.args[0]
    assert inbound.wa_id == "34600111222"
    assert inbound.wa_message_id == "wamid.AAAA"
    assert inbound.text == "hello"


async def test_post_with_invalid_signature_does_not_process(
    client: AsyncClient,
) -> None:
    payload = _text_message_payload()
    body = json.dumps(payload).encode()

    with patch(
        "app.routers.webhooks.process_inbound_message",
        new=AsyncMock(return_value=None),
    ) as mock_process:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=deadbeef",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    mock_process.assert_not_called()


async def test_post_without_signature_header_does_not_process(
    client: AsyncClient,
) -> None:
    payload = _text_message_payload()
    body = json.dumps(payload).encode()

    with patch(
        "app.routers.webhooks.process_inbound_message",
        new=AsyncMock(return_value=None),
    ) as mock_process:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    mock_process.assert_not_called()


async def test_post_status_webhook_is_silently_ignored(client: AsyncClient) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "statuses": [
                                {
                                    "id": "wamid.OUT",
                                    "status": "delivered",
                                    "timestamp": "1700000000",
                                    "recipient_id": "34600111222",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()

    with patch(
        "app.routers.webhooks.process_inbound_message",
        new=AsyncMock(return_value=None),
    ) as mock_process:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    mock_process.assert_not_called()


async def test_post_non_text_message_is_acknowledged_but_not_processed(
    client: AsyncClient,
) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": "34600111222",
                                    "id": "wamid.IMG",
                                    "timestamp": "1700000000",
                                    "type": "image",
                                    "image": {"id": "img-id"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()

    with patch(
        "app.routers.webhooks.process_inbound_message",
        new=AsyncMock(return_value=None),
    ) as mock_process:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    mock_process.assert_not_called()


async def test_post_malformed_json_returns_200(client: AsyncClient) -> None:
    body = b"{not valid json"
    response = await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "received"}


# --- _parse_inbound_messages unit tests -------------------------------------


def test_parse_returns_empty_list_for_empty_payload() -> None:
    assert _parse_inbound_messages({}) == []


def test_parse_extracts_text_message_fields() -> None:
    payload = _text_message_payload(
        wa_id="34600111222", wa_message_id="wamid.X", text="hi there"
    )
    [inbound] = _parse_inbound_messages(payload)
    assert inbound.wa_id == "34600111222"
    assert inbound.wa_message_id == "wamid.X"
    assert inbound.text == "hi there"
    assert inbound.phone_number_id == "1234567890"
    assert inbound.interactive_id is None


def _interactive_payload(
    *,
    interactive_type: str,
    reply_id: str,
    reply_title: str = "Pick me",
    wa_id: str = "34600111222",
    wa_message_id: str = "wamid.IR",
) -> dict[str, Any]:
    reply_key = "button_reply" if interactive_type == "button_reply" else "list_reply"
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": wa_message_id,
                                    "timestamp": "1700000000",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": interactive_type,
                                        reply_key: {
                                            "id": reply_id,
                                            "title": reply_title,
                                        },
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_parse_extracts_button_reply_interactive_id() -> None:
    payload = _interactive_payload(
        interactive_type="button_reply",
        reply_id="disambig:abc:1",
        reply_title="Project meeting",
    )
    [inbound] = _parse_inbound_messages(payload)
    assert inbound.interactive_id == "disambig:abc:1"
    assert inbound.text == "Project meeting"


def test_parse_extracts_list_reply_interactive_id() -> None:
    payload = _interactive_payload(
        interactive_type="list_reply",
        reply_id="disambig:abc:3",
        reply_title="Dentist",
    )
    [inbound] = _parse_inbound_messages(payload)
    assert inbound.interactive_id == "disambig:abc:3"
    assert inbound.text == "Dentist"


def test_parse_skips_unknown_interactive_type() -> None:
    payload = _interactive_payload(
        interactive_type="nfm_reply", reply_id="x", reply_title="y"
    )
    assert _parse_inbound_messages(payload) == []
