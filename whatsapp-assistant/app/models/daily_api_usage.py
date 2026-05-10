from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DailyApiUsage(Base):
    __tablename__ = "daily_api_usage"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<DailyApiUsage usage_date={self.usage_date!r} "
            f"request_count={self.request_count}>"
        )

