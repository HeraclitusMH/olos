from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models import Message
from app.services.context import _role_for_direction


def test_role_for_direction_maps_whatsapp_direction_to_openai_role() -> None:
    assert _role_for_direction("inbound") == "user"
    assert _role_for_direction("outbound") == "assistant"
    assert _role_for_direction("system") is None


def test_message_can_store_tool_calls_json() -> None:
    message = Message(
        user_id=uuid4(),
        direction="inbound",
        content="hello",
        tool_calls_json={"tool_calls": [{"name": "reply", "arguments": {}}]},
        created_at=datetime.now(UTC),
    )

    assert message.tool_calls_json["tool_calls"][0]["name"] == "reply"
