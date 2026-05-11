from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class DailyAgendaSend(Base):
    __tablename__ = "daily_agenda_sends"
    __table_args__ = (
        UniqueConstraint("user_id", "agenda_date", name="uq_daily_agenda_user_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_daily_agenda_sends_user_id"),
        nullable=False,
    )
    agenda_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduled_time_local: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    user: Mapped[User] = relationship("User", lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<DailyAgendaSend user_id={self.user_id} "
            f"agenda_date={self.agenda_date!r}>"
        )
