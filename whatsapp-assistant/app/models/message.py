from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.event_reference import EventReference
    from app.models.user import User


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_user_id_created_at", "user_id", "created_at"),
        Index("ix_messages_processed_created_at", "processed", "created_at"),
    )

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
    wa_message_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    execution_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    user: Mapped[User] = relationship(
        "User", back_populates="messages", lazy="raise"
    )
    event_references: Mapped[list[EventReference]] = relationship(
        "EventReference", back_populates="created_from_message", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Message id={self.id} direction={self.direction!r} processed={self.processed}>"
