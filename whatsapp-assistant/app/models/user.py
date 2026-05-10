from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.event_reference import EventReference
    from app.models.google_account import GoogleAccount
    from app.models.memory import Memory
    from app.models.message import Message


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    wa_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'Europe/Madrid'")
    )
    locale: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'en'")
    )
    preferences_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    google_accounts: Mapped[list[GoogleAccount]] = relationship(
        "GoogleAccount", back_populates="user", lazy="raise"
    )
    messages: Mapped[list[Message]] = relationship(
        "Message", back_populates="user", lazy="raise"
    )
    event_references: Mapped[list[EventReference]] = relationship(
        "EventReference", back_populates="user", lazy="raise"
    )
    memories: Mapped[list[Memory]] = relationship(
        "Memory", back_populates="user", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} wa_id={self.wa_id!r}>"
