from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.models import Memory
from app.services.message_processor import _process_interactive_reply
from app.services.tool_executor import ToolExecutor


class _DummyUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.wa_id = "34600111222"
        self.timezone = "Europe/Madrid"
        self.preferences_json: dict[str, Any] = {}


class _NoOpSession:
    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
        raise AssertionError("session not used directly")

    def add(self, _obj: Any) -> None:  # pragma: no cover
        pass

    async def flush(self) -> None:  # pragma: no cover
        pass


class _FakeMemoryService:
    def __init__(
        self,
        *,
        get_by_id_returns: Memory | None = None,
    ) -> None:
        self._get_by_id_returns = get_by_id_returns
        self.forget_calls: list[Memory] = []
        self.update_calls: list[dict[str, Any]] = []
        self.get_by_id_calls: list[uuid.UUID] = []

    async def forget(self, *, memory: Memory, db: Any) -> Memory:
        self.forget_calls.append(memory)
        memory.deleted_at = datetime.now(timezone.utc)
        return memory

    async def update_content(
        self, *, memory: Memory, new_content: str, new_tags: Any, db: Any
    ) -> Memory:
        self.update_calls.append(
            {
                "memory_id": memory.id,
                "new_content": new_content,
                "new_tags": new_tags,
            }
        )
        memory.content = new_content
        memory.tags = list(new_tags) if new_tags else None
        return memory

    async def get_by_id(
        self, memory_id: uuid.UUID, user_id: uuid.UUID, db: Any
    ) -> Memory | None:
        self.get_by_id_calls.append(memory_id)
        return self._get_by_id_returns


def _memory(user_id: uuid.UUID, content: str) -> Memory:
    memory = Memory(user_id=user_id, content=content, tags=None)
    memory.id = uuid.uuid4()
    return memory


# --- forget confirmation flow ---------------------------------------------


def _park_forget_pending(user: _DummyUser, memory_id: str) -> None:
    user.preferences_json = {
        "pending_action": {
            "id": memory_id,
            "type": "memory_forget",
            "arguments": {"memory_id": memory_id},
            "options": [],
            "memory_id": memory_id,
            "expires_at": "2999-01-01T00:00:00+00:00",
        }
    }


async def test_confirm_forget_button_soft_deletes_memory() -> None:
    user = _DummyUser()
    memory = _memory(user.id, "Indonesian number")
    _park_forget_pending(user, str(memory.id))
    fake = _FakeMemoryService(get_by_id_returns=memory)

    log = await _process_interactive_reply(
        interactive_id=f"confirm_forget_{memory.id}",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=ToolExecutor(),
        memory_service=fake,  # type: ignore[arg-type]
    )

    assert log.combined_reply == "Forgotten."
    assert fake.forget_calls == [memory]
    assert "pending_action" not in user.preferences_json


async def test_cancel_forget_button_keeps_memory_and_clears_pending() -> None:
    user = _DummyUser()
    memory = _memory(user.id, "Indonesian number")
    _park_forget_pending(user, str(memory.id))
    fake = _FakeMemoryService(get_by_id_returns=memory)

    log = await _process_interactive_reply(
        interactive_id=f"cancel_forget_{memory.id}",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=ToolExecutor(),
        memory_service=fake,  # type: ignore[arg-type]
    )

    assert log.combined_reply == "Okay, keeping it."
    assert fake.forget_calls == []
    assert "pending_action" not in user.preferences_json


async def test_confirm_forget_with_mismatched_pending_returns_expired() -> None:
    user = _DummyUser()
    other_id = str(uuid.uuid4())
    _park_forget_pending(user, other_id)
    fake = _FakeMemoryService()

    log = await _process_interactive_reply(
        interactive_id=f"confirm_forget_{uuid.uuid4()}",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=ToolExecutor(),
        memory_service=fake,  # type: ignore[arg-type]
    )

    assert "expired" in log.combined_reply.lower()
    assert fake.forget_calls == []


async def test_confirm_forget_with_no_pending_returns_expired() -> None:
    user = _DummyUser()
    fake = _FakeMemoryService()

    log = await _process_interactive_reply(
        interactive_id=f"confirm_forget_{uuid.uuid4()}",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=ToolExecutor(),
        memory_service=fake,  # type: ignore[arg-type]
    )

    assert "expired" in log.combined_reply.lower()


async def test_confirm_forget_when_memory_already_gone_returns_unavailable() -> None:
    user = _DummyUser()
    memory_id = str(uuid.uuid4())
    _park_forget_pending(user, memory_id)
    fake = _FakeMemoryService(get_by_id_returns=None)

    log = await _process_interactive_reply(
        interactive_id=f"confirm_forget_{memory_id}",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=ToolExecutor(),
        memory_service=fake,  # type: ignore[arg-type]
    )

    assert "no longer available" in log.combined_reply
    assert fake.forget_calls == []
    assert "pending_action" not in user.preferences_json


# --- memory_update disambiguation flow ------------------------------------


async def test_memory_update_disambig_button_updates_selected_memory() -> None:
    user = _DummyUser()
    selected_id = uuid.uuid4()
    other_id = uuid.uuid4()
    user.preferences_json = {
        "pending_action": {
            "id": "abc",
            "type": "memory_update",
            "arguments": {
                "query": "indonesian",
                "new_content": "+62 813 9999 0000",
            },
            "options": [
                {"memory_id": str(other_id), "snippet": "..."},
                {"memory_id": str(selected_id), "snippet": "..."},
            ],
            "expires_at": "2999-01-01T00:00:00+00:00",
        }
    }
    target = Memory(user_id=user.id, content="Indonesian number is +62 812", tags=None)
    target.id = selected_id
    fake = _FakeMemoryService(get_by_id_returns=target)

    log = await _process_interactive_reply(
        interactive_id="disambig:abc:1",
        user=user,  # type: ignore[arg-type]
        session=_NoOpSession(),  # type: ignore[arg-type]
        inbound_message_id=uuid.uuid4(),
        tool_executor=ToolExecutor(),
        memory_service=fake,  # type: ignore[arg-type]
    )

    assert "Updated:" in log.combined_reply
    assert fake.update_calls[0]["memory_id"] == selected_id
    assert fake.update_calls[0]["new_content"] == "+62 813 9999 0000"
    assert "pending_action" not in user.preferences_json
