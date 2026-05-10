from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.models import Memory
from app.services.memory import MemoryService


class _FakeResult:
    """Mimics SQLAlchemy ``Result`` for the patterns ``MemoryService`` uses."""

    def __init__(
        self,
        rows: list[Any] | None = None,
        scalar_rows: list[Any] | None = None,
    ) -> None:
        self._rows = rows or []
        self._scalar_rows = scalar_rows if scalar_rows is not None else self._rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> "_FakeScalars":
        return _FakeScalars(self._scalar_rows)


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Programmable session that returns a queue of canned results."""

    def __init__(self, results: list[_FakeResult] | None = None) -> None:
        self._results = list(results or [])
        self.added: list[Any] = []
        self.flushed = 0
        self.executed_statements: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # Mirror the trigger: we don't simulate search_vector; just give it an id.
        if isinstance(obj, Memory) and obj.id is None:
            obj.id = uuid.uuid4()

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, statement: Any) -> _FakeResult:
        self.executed_statements.append(statement)
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


def _memory(
    *,
    user_id: uuid.UUID,
    content: str,
    tags: list[str] | None = None,
    deleted: bool = False,
) -> Memory:
    memory = Memory(user_id=user_id, content=content, tags=tags)
    memory.id = uuid.uuid4()
    if deleted:
        memory.deleted_at = datetime.now(timezone.utc)
    return memory


# --- store -----------------------------------------------------------------


async def test_store_inserts_memory_and_flushes() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    session = _FakeSession()

    memory = await service.store(
        user_id=user_id,
        content="Indonesian number is +62 812 3456 7890",
        tags=["phone", "indonesia", "number"],
        db=session,  # type: ignore[arg-type]
    )

    assert memory.user_id == user_id
    assert memory.content == "Indonesian number is +62 812 3456 7890"
    assert memory.tags == ["phone", "indonesia", "number"]
    assert session.added == [memory]
    assert session.flushed == 1


async def test_store_with_no_tags_stores_none() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    session = _FakeSession()

    memory = await service.store(
        user_id=user_id, content="x", tags=None, db=session  # type: ignore[arg-type]
    )

    assert memory.tags is None


# --- retrieve --------------------------------------------------------------


async def test_retrieve_returns_full_text_match() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    fts_hit = _memory(
        user_id=user_id, content="My Indonesian phone number is +62 812"
    )
    session = _FakeSession(
        [
            _FakeResult(rows=[(fts_hit, 0.9)]),
            _FakeResult(scalar_rows=[]),  # tag-overlap query
        ]
    )

    matches = await service.retrieve(
        user_id=user_id,
        query="What's my Indonesian phone number?",
        db=session,  # type: ignore[arg-type]
    )

    assert matches == [fts_hit]
    # Two queries: FTS + tag overlap. Fallback ILIKE not run.
    assert len(session.executed_statements) == 2


async def test_retrieve_combines_fts_and_tag_results_dedup_in_order() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    a = _memory(user_id=user_id, content="A")
    b = _memory(user_id=user_id, content="B")
    c = _memory(user_id=user_id, content="C")
    session = _FakeSession(
        [
            _FakeResult(rows=[(a, 0.9), (b, 0.7)]),
            _FakeResult(scalar_rows=[b, c]),  # b is duplicate
        ]
    )

    matches = await service.retrieve(
        user_id=user_id,
        query="phone number tag",
        db=session,  # type: ignore[arg-type]
        limit=3,
    )

    assert matches == [a, b, c]


async def test_retrieve_falls_back_to_ilike_when_no_fts_or_tag_hits() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    fallback_hit = _memory(user_id=user_id, content="Boss RC-505 loop station")
    session = _FakeSession(
        [
            _FakeResult(rows=[]),  # FTS empty
            _FakeResult(scalar_rows=[]),  # Tag empty
            _FakeResult(scalar_rows=[fallback_hit]),  # ILIKE hit
        ]
    )

    matches = await service.retrieve(
        user_id=user_id,
        query="loop station",
        db=session,  # type: ignore[arg-type]
    )

    assert matches == [fallback_hit]
    assert len(session.executed_statements) == 3


async def test_retrieve_returns_empty_for_only_stopwords() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    session = _FakeSession()

    matches = await service.retrieve(
        user_id=user_id,
        query="what is it",
        db=session,  # type: ignore[arg-type]
    )

    assert matches == []
    # No queries fired — short-circuited.
    assert session.executed_statements == []


# --- update_content --------------------------------------------------------


async def test_update_content_replaces_content_tags_and_flushes() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    memory = _memory(user_id=user_id, content="old content", tags=["old"])
    session = _FakeSession()

    await service.update_content(
        memory=memory,
        new_content="new content",
        new_tags=["new", "tag"],
        db=session,  # type: ignore[arg-type]
    )

    assert memory.content == "new content"
    assert memory.tags == ["new", "tag"]
    assert memory.updated_at is not None
    assert session.flushed == 1


# --- forget ----------------------------------------------------------------


async def test_forget_sets_deleted_at_without_hard_deleting() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    memory = _memory(user_id=user_id, content="secret")
    session = _FakeSession()

    await service.forget(memory=memory, db=session)  # type: ignore[arg-type]

    assert memory.deleted_at is not None
    assert session.flushed == 1


# --- get_by_id -------------------------------------------------------------


async def test_get_by_id_returns_memory_when_present() -> None:
    service = MemoryService()
    user_id = uuid.uuid4()
    memory = _memory(user_id=user_id, content="x")
    session = _FakeSession([_FakeResult(scalar_rows=[memory])])

    found = await service.get_by_id(
        memory.id, user_id, session  # type: ignore[arg-type]
    )

    assert found is memory


async def test_get_by_id_returns_none_when_missing() -> None:
    service = MemoryService()
    session = _FakeSession([_FakeResult(scalar_rows=[])])

    found = await service.get_by_id(
        uuid.uuid4(), uuid.uuid4(), session  # type: ignore[arg-type]
    )

    assert found is None
