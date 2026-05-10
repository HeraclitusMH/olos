from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.message import Message
    from app.models.user import User


class EventReference(Base):
    __tablename__ = "event_references"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    google_calendar_id: Mapped[str] = mapped_column(String, nullable=False)
    google_event_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_from_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    user: Mapped[User] = relationship(
        "User", back_populates="event_references", lazy="raise"
    )
    created_from_message: Mapped[Message | None] = relationship(
        "Message", back_populates="event_references", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<EventReference id={self.id} google_event_id={self.google_event_id!r}>"
