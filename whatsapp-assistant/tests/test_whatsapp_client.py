from __future__ import annotations

import json

import httpx
import pytest

from app.services.whatsapp import WhatsAppAPIError, WhatsAppClient


def _client_with_transport(transport: httpx.MockTransport) -> WhatsAppClient:
    """Build a WhatsAppClient that uses a mocked transport.

    Patches ``httpx.AsyncClient`` for the duration of one ``_post`` call.
    """

    class _StubClient(WhatsAppClient):
        async def _post(self, payload):  # type: ignore[override]
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post(
                    self._messages_url, headers=self._headers, json=payload
                )
            if response.status_code >= 400:
                raise WhatsAppAPIError(response.status_code, response.text)
            return response.json()

    return _StubClient(access_token="tok", phone_number_id="PHONE_ID")


async def test_send_text_message_posts_correct_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"messages": [{"id": "wamid.OUT_1"}]}
        )

    transport = httpx.MockTransport(handler)
    client = _client_with_transport(transport)

    result = await client.send_text_message("34600111222", "hi")

    assert result == {"messages": [{"id": "wamid.OUT_1"}]}
    assert captured["url"].endswith("/v19.0/PHONE_ID/messages")
    assert captured["headers"]["authorization"] == "Bearer tok"
    assert captured["body"] == {
        "messaging_product": "whatsapp",
        "to": "34600111222",
        "type": "text",
        "text": {"body": "hi"},
    }


async def test_send_text_message_raises_on_error_status() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {"message": "bad"}})
    )
    client = _client_with_transport(transport)

    with pytest.raises(WhatsAppAPIError) as exc_info:
        await client.send_text_message("34600111222", "hi")

    assert exc_info.value.status_code == 400


async def test_send_interactive_buttons_payload_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    transport = httpx.MockTransport(handler)
    client = _client_with_transport(transport)

    buttons = [
        {"type": "reply", "reply": {"id": "btn_1", "title": "Yes"}},
        {"type": "reply", "reply": {"id": "btn_2", "title": "No"}},
    ]
    await client.send_interactive_buttons("34600", "Confirm?", buttons)

    interactive = captured["body"]["interactive"]
    assert captured["body"]["type"] == "interactive"
    assert interactive["type"] == "button"
    assert interactive["body"] == {"text": "Confirm?"}
    assert interactive["action"]["buttons"] == buttons


async def test_send_template_message_payload_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    transport = httpx.MockTransport(handler)
    client = _client_with_transport(transport)

    await client.send_template_message(
        "34600111222", "daily_agenda", "Good morning! You have no events today.", "en_US"
    )

    body = captured["body"]
    assert body["type"] == "template"
    assert body["to"] == "34600111222"
    tmpl = body["template"]
    assert tmpl["name"] == "daily_agenda"
    assert tmpl["language"] == {"code": "en_US"}
    components = tmpl["components"]
    assert len(components) == 1
    assert components[0]["type"] == "body"
    assert components[0]["parameters"] == [
        {"type": "text", "text": "Good morning! You have no events today."}
    ]


async def test_send_interactive_list_payload_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    transport = httpx.MockTransport(handler)
    client = _client_with_transport(transport)

    sections = [
        {"title": "Choices", "rows": [{"id": "r1", "title": "A"}]},
    ]
    await client.send_interactive_list("34600", "Pick one", "Choose", sections)

    interactive = captured["body"]["interactive"]
    assert interactive["type"] == "list"
    assert interactive["action"] == {"button": "Choose", "sections": sections}
