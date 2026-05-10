from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.models import Memory
from app.services.tool_executor import ToolExecutor


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = {}


class _NoOpSession:
    """Fake session — memory tests don't exercise the real DB layer."""

    def __init__(self) -> None:
        self.flushed = 0

    def add(self, _obj: Any) -> None:  # pragma: no cover - not exercised here
        pass

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
        raise AssertionError("session not used directly")


class _FakeMemoryService:
    """Drop-in replacement for ``MemoryService`` that records calls."""

    def __init__(
        self,
        *,
        retrieve_returns: list[Memory] | None = None,
        store_factory: Any = None,
        get_by_id_returns: Memory | None = None,
    ) -> None:
        self._retrieve_returns = retrieve_returns or []
        self._store_factory = store_factory
        self._get_by_id_returns = get_by_id_returns
        self.calls: dict[str, list[dict[str, Any]]] = {
            "store": [],
            "retrieve": [],
            "update_content": [],
            "forget": [],
            "get_by_id": [],
        }

    async def store(self, *, user_id: uuid.UUID, content: str, tags: Any, db: Any) -> Memory:
        self.calls["store"].append(
            {"user_id": user_id, "content": content, "tags": tags}
        )
        if callable(self._store_factory):
            return self._store_factory(user_id=user_id, content=content, tags=tags)
        memory = Memory(user_id=user_id, content=content, tags=tags)
        memory.id = uuid.uuid4()
        return memory

    async def retrieve(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        db: Any,
        limit: int = 3,
    ) -> list[Memory]:
        self.calls["retrieve"].append(
            {"user_id": user_id, "query": query, "limit": limit}
        )
        return list(self._retrieve_returns)

    async def update_content(
        self, *, memory: Memory, new_content: str, new_tags: Any, db: Any
    ) -> Memory:
        self.calls["update_content"].append(
            {
                "memory_id": memory.id,
                "new_content": new_content,
                "new_tags": new_tags,
            }
        )
        memory.content = new_content
        memory.tags = list(new_tags) if new_tags else None
        memory.updated_at = datetime.now(timezone.utc)
        return memory

    async def forget(self, *, memory: Memory, db: Any) -> Memory:
        self.calls["forget"].append({"memory_id": memory.id})
        memory.deleted_at = datetime.now(timezone.utc)
        return memory

    async def get_by_id(
        self, memory_id: uuid.UUID, user_id: uuid.UUID, db: Any
    ) -> Memory | None:
        self.calls["get_by_id"].append(
            {"memory_id": memory_id, "user_id": user_id}
        )
        return self._get_by_id_returns


class _FakeWhatsApp:
    def __init__(self) -> None:
        self.button_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def send_interactive_buttons(
        self, to: str, body_text: str, buttons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.button_calls.append(
            {"to": to, "body_text": body_text, "buttons": buttons}
        )
        return {"ok": True}

    async def send_interactive_list(
        self,
        to: str,
        body_text: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.list_calls.append(
            {
                "to": to,
                "body_text": body_text,
                "button_text": button_text,
                "sections": sections,
            }
        )
        return {"ok": True}


def _memory(user_id: uuid.UUID, content: str, *, tags: list[str] | None = None) -> Memory:
    memory = Memory(user_id=user_id, content=content, tags=tags)
    memory.id = uuid.uuid4()
    return memory


def _build(
    *,
    memory_service: _FakeMemoryService,
    whatsapp: _FakeWhatsApp | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        memory_service_factory=lambda: memory_service,
        whatsapp_client_factory=lambda: whatsapp or _FakeWhatsApp(),
    )


# --- memory_store ----------------------------------------------------------


async def test_memory_store_persists_and_returns_short_summary() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService()
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_store",
        {
            "content": "My Indonesian number is +62 812 3456 7890",
            "tags": ["phone", "indonesia", "number"],
        },
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message.startswith("Saved: My Indonesian number is +62 812 3456")
    stored = fake.calls["store"][0]
    assert stored["content"] == "My Indonesian number is +62 812 3456 7890"
    assert stored["tags"] == ["phone", "indonesia", "number"]


async def test_memory_store_truncates_long_content_summary() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService()
    executor = _build(memory_service=fake)
    long_content = "A" * 80

    result = await executor.execute(
        "memory_store",
        {"content": long_content, "tags": ["x", "y"]},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.message == f"Saved: {'A' * 50}..."


async def test_memory_store_rejects_empty_content() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService()
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_store",
        {"content": "   ", "tags": ["x"]},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert fake.calls["store"] == []


# --- memory_retrieve -------------------------------------------------------


async def test_memory_retrieve_single_match_returns_content() -> None:
    user = _DummyUser()
    memory = _memory(user.id, "+62 812 3456 7890")
    fake = _FakeMemoryService(retrieve_returns=[memory])
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_retrieve",
        {"query": "What's my Indonesian phone number?"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == "+62 812 3456 7890"


async def test_memory_retrieve_multiple_matches_returns_numbered_list() -> None:
    user = _DummyUser()
    a = _memory(user.id, "Boss RC-505")
    b = _memory(user.id, "Maybe also a Headrush Looperboard")
    fake = _FakeMemoryService(retrieve_returns=[a, b])
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_retrieve",
        {"query": "What loop station did I want?"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert "1. Boss RC-505" in result.message
    assert "2. Maybe also a Headrush Looperboard" in result.message


async def test_memory_retrieve_no_matches_returns_default_message() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService(retrieve_returns=[])
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_retrieve",
        {"query": "What did I have for breakfast in 2014?"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message == "I do not have anything stored about that."


# --- memory_update ---------------------------------------------------------


async def test_memory_update_single_match_replaces_content() -> None:
    user = _DummyUser()
    target = _memory(user.id, "Indonesian number is +62 812 3456 7890")
    fake = _FakeMemoryService(retrieve_returns=[target])
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_update",
        {
            "query": "indonesian phone",
            "new_content": "Indonesian number is +62 813 9999 0000",
        },
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.message.startswith("Updated:")
    assert "+62 813 9999 0000" in result.message
    assert fake.calls["update_content"][0]["new_content"] == (
        "Indonesian number is +62 813 9999 0000"
    )


async def test_memory_update_no_match_returns_friendly_error() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService(retrieve_returns=[])
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_update",
        {"query": "nonexistent", "new_content": "y"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.message == "Could not find a memory matching that."


async def test_memory_update_multiple_matches_sends_disambiguation_buttons() -> None:
    user = _DummyUser()
    a = _memory(user.id, "Indonesian number is +62 812 3456 7890")
    b = _memory(user.id, "Indonesian work number is +62 555 0000")
    fake = _FakeMemoryService(retrieve_returns=[a, b])
    whatsapp = _FakeWhatsApp()
    executor = _build(memory_service=fake, whatsapp=whatsapp)

    result = await executor.execute(
        "memory_update",
        {"query": "indonesian", "new_content": "+62 813 9999 0000"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.data["sent_directly"] is True
    assert len(whatsapp.button_calls) == 1
    pending = user.preferences_json["pending_action"]
    assert pending["type"] == "memory_update"
    assert pending["arguments"]["new_content"] == "+62 813 9999 0000"
    assert len(pending["options"]) == 2
    assert pending["options"][0]["memory_id"] == str(a.id)


# --- memory_forget ---------------------------------------------------------


async def test_memory_forget_single_match_sends_confirmation_buttons() -> None:
    user = _DummyUser()
    target = _memory(user.id, "Indonesian number is +62 812 3456 7890")
    fake = _FakeMemoryService(retrieve_returns=[target])
    whatsapp = _FakeWhatsApp()
    executor = _build(memory_service=fake, whatsapp=whatsapp)

    result = await executor.execute(
        "memory_forget",
        {"query": "indonesian phone"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.data["sent_directly"] is True
    assert fake.calls["forget"] == []
    sent = whatsapp.button_calls[0]
    assert "Are you sure" in sent["body_text"]
    button_ids = [b["reply"]["id"] for b in sent["buttons"]]
    assert button_ids == [
        f"confirm_forget_{target.id}",
        f"cancel_forget_{target.id}",
    ]
    pending = user.preferences_json["pending_action"]
    assert pending["type"] == "memory_forget"
    assert pending["memory_id"] == str(target.id)


async def test_memory_forget_no_match_returns_friendly_error() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService(retrieve_returns=[])
    executor = _build(memory_service=fake)

    result = await executor.execute(
        "memory_forget",
        {"query": "nonexistent"},
        user=user,  # type: ignore[arg-type]
        db=_NoOpSession(),  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.message == "Could not find a memory matching that."
