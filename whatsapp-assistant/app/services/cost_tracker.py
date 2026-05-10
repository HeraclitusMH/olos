"""Daily OpenAI API request counter."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import DailyApiUsage


class CostTracker:
    async def increment(self, db: AsyncSession) -> None:
        today = _today()
        stmt = (
            insert(DailyApiUsage)
            .values(usage_date=today, request_count=1)
            .on_conflict_do_update(
                index_elements=[DailyApiUsage.usage_date],
                set_={
                    "request_count": DailyApiUsage.request_count + 1,
                    "updated_at": func.now(),
                },
            )
        )
        await db.execute(stmt)
        await db.flush()

    async def check_limit(self, db: AsyncSession) -> bool:
        return await self.get_today_count(db) < get_settings().daily_api_limit

    async def get_today_count(self, db: AsyncSession) -> int:
        result = await db.execute(
            select(DailyApiUsage.request_count).where(
                DailyApiUsage.usage_date == _today()
            )
        )
        return result.scalar_one_or_none() or 0


def _today() -> date:
    return datetime.now(UTC).date()

