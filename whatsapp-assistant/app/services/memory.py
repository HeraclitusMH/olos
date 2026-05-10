"""Memory storage and retrieval backed by Postgres full-text search.

The ``memories`` table has a ``search_vector`` (TSVECTOR) column kept in
sync with ``content`` and ``tags`` by a database trigger
(see ``alembic/versions/0001_initial_schema.py``). Retrieval combines an
``@@ plainto_tsquery`` match (ranked by ``ts_rank``) with a tag overlap
(``&&``) lookup, falling back to a literal ``ILIKE`` on the content when
neither produces hits.

All queries filter ``deleted_at IS NULL`` — soft deletes are the only
way to remove a memory.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory
from app.utils.search import build_search_query

logger = logging.getLogger(__name__)


class MemoryService:
    """CRUD + search for ``Memory`` rows."""

    async def store(
        self,
        user_id: uuid.UUID,
        content: str,
        tags: list[str] | None,
        db: AsyncSession,
    ) -> Memory:
        """Insert a new memory. ``search_vector`` is populated by the trigger."""
        memory = Memory(
            user_id=user_id,
            content=content,
            tags=list(tags) if tags else None,
        )
        db.add(memory)
        await db.flush()
        return memory

    async def retrieve(
        self,
        user_id: uuid.UUID,
        query: str,
        db: AsyncSession,
        limit: int = 3,
    ) -> list[Memory]:
        """Return up to ``limit`` matching memories ordered by relevance.

        Strategy:
        1. ``plainto_tsquery('english', cleaned)`` against ``search_vector``,
           ordered by ``ts_rank`` desc.
        2. Tag overlap (``&&``) using the cleaned tokens.
        3. Combine + dedupe — full-text matches lead.
        4. Fallback to ``ILIKE %query%`` on content if neither path matched.
        """
        cleaned = build_search_query(query)
        if not cleaned:
            return []

        tsquery = func.plainto_tsquery("english", cleaned)
        rank = func.ts_rank(Memory.search_vector, tsquery).label("rank")

        fts_stmt = (
            select(Memory, rank)
            .where(
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
                Memory.search_vector.op("@@")(tsquery),
            )
            .order_by(rank.desc())
            .limit(limit * 2)
        )
        fts_result = await db.execute(fts_stmt)
        fts_rows = list(fts_result.all())

        terms = [t for t in cleaned.split() if t]
        tag_rows: list[Memory] = []
        if terms:
            tag_stmt = (
                select(Memory)
                .where(
                    Memory.user_id == user_id,
                    Memory.deleted_at.is_(None),
                    Memory.tags.op("&&")(cast(terms, ARRAY(String))),
                )
                .limit(limit * 2)
            )
            tag_result = await db.execute(tag_stmt)
            tag_rows = list(tag_result.scalars().all())

        combined: dict[uuid.UUID, Memory] = {}
        for memory, _rank_value in fts_rows:
            combined[memory.id] = memory
        for memory in tag_rows:
            if memory.id not in combined:
                combined[memory.id] = memory

        if combined:
            return list(combined.values())[:limit]

        ilike_stmt = (
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
                Memory.content.ilike(f"%{cleaned}%"),
            )
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        ilike_result = await db.execute(ilike_stmt)
        return list(ilike_result.scalars().all())

    async def update_content(
        self,
        memory: Memory,
        new_content: str,
        new_tags: list[str] | None,
        db: AsyncSession,
    ) -> Memory:
        """Apply new content/tags to an already-loaded memory.

        The trigger refreshes ``search_vector`` on UPDATE.
        """
        memory.content = new_content
        if new_tags is not None:
            memory.tags = list(new_tags) if new_tags else None
        memory.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return memory

    async def forget(
        self,
        memory: Memory,
        db: AsyncSession,
    ) -> Memory:
        """Soft-delete: set ``deleted_at = now()``. Never hard-delete."""
        memory.deleted_at = datetime.now(timezone.utc)
        await db.flush()
        return memory

    async def get_by_id(
        self,
        memory_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Memory | None:
        """Return the active (non-deleted) memory belonging to ``user_id``."""
        stmt = select(Memory).where(
            Memory.id == memory_id,
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        return result.scalars().first()
