"""Schema/mapping tests for ORM models. No live DB required."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Text,
    inspect,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.database import Base
from app.models import EventReference, GoogleAccount, Memory, Message, User


# ---------- metadata registration ----------


def test_all_models_registered_in_metadata() -> None:
    assert set(Base.metadata.tables.keys()) == {
        "users",
        "messages",
        "google_accounts",
        "event_references",
        "memories",
    }


# ---------- User ----------


def test_user_tablename() -> None:
    assert User.__tablename__ == "users"


def test_user_id_is_uuid_pk_with_gen_random_uuid_default() -> None:
    col = User.__table__.c.id
    assert col.primary_key is True
    assert isinstance(col.type, PG_UUID)
    assert col.server_default.arg.text == "gen_random_uuid()"


def test_user_wa_id_is_unique_not_null_indexed() -> None:
    col = User.__table__.c.wa_id
    assert isinstance(col.type, String)
    assert col.nullable is False
    assert col.unique is True
    assert col.index is True


def test_user_timezone_default_is_europe_madrid() -> None:
    col = User.__table__.c.timezone
    assert col.nullable is False
    assert col.server_default.arg.text == "'Europe/Madrid'"


def test_user_locale_default_is_en() -> None:
    col = User.__table__.c.locale
    assert col.nullable is False
    assert col.server_default.arg.text == "'en'"


def test_user_preferences_json_is_jsonb_with_empty_default() -> None:
    col = User.__table__.c.preferences_json
    assert isinstance(col.type, JSONB)
    assert col.nullable is False
    assert col.server_default.arg.text == "'{}'::jsonb"


def test_user_created_at_is_tz_aware_with_now_default() -> None:
    col = User.__table__.c.created_at
    assert isinstance(col.type, DateTime)
    assert col.type.timezone is True
    assert col.nullable is False
    assert col.server_default.arg.text == "now()"


def test_user_updated_at_is_nullable_with_onupdate() -> None:
    col = User.__table__.c.updated_at
    assert col.nullable is True
    assert col.onupdate is not None


def test_user_relationships_declared() -> None:
    keys = {r.key for r in inspect(User).relationships}
    assert keys == {"google_accounts", "messages", "event_references", "memories"}


def test_user_repr_includes_wa_id() -> None:
    u = User(wa_id="+34123456789")
    u.id = uuid.uuid4()
    out = repr(u)
    assert "User" in out
    assert "+34123456789" in out


# ---------- Message ----------


def test_message_tablename() -> None:
    assert Message.__tablename__ == "messages"


def test_message_user_id_fk_to_users() -> None:
    col = Message.__table__.c.user_id
    assert col.nullable is False
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"
    assert fks[0].column.name == "id"


def test_message_wa_message_id_is_nullable_unique() -> None:
    col = Message.__table__.c.wa_message_id
    assert col.nullable is True
    assert col.unique is True


def test_message_direction_is_required_string() -> None:
    col = Message.__table__.c.direction
    assert isinstance(col.type, String)
    assert col.nullable is False


def test_message_processed_defaults_to_false() -> None:
    col = Message.__table__.c.processed
    assert isinstance(col.type, Boolean)
    assert col.nullable is False
    assert col.server_default.arg.text == "false"


def test_message_jsonb_columns_are_nullable() -> None:
    for name in ("tool_calls_json", "execution_result_json"):
        col = Message.__table__.c[name]
        assert isinstance(col.type, JSONB)
        assert col.nullable is True


def test_message_text_and_int_columns() -> None:
    assert isinstance(Message.__table__.c.content.type, Text)
    assert isinstance(Message.__table__.c.error.type, Text)
    assert isinstance(Message.__table__.c.processing_duration_ms.type, Integer)


def test_message_composite_indexes_declared() -> None:
    names = {idx.name for idx in Message.__table__.indexes}
    assert "ix_messages_user_id_created_at" in names
    assert "ix_messages_processed_created_at" in names


def test_message_relationships_declared() -> None:
    keys = {r.key for r in inspect(Message).relationships}
    assert keys == {"user", "event_references"}


def test_message_repr_includes_direction() -> None:
    m = Message(direction="inbound", processed=False)
    m.id = uuid.uuid4()
    out = repr(m)
    assert "Message" in out
    assert "inbound" in out


# ---------- GoogleAccount ----------


def test_google_account_tablename() -> None:
    assert GoogleAccount.__tablename__ == "google_accounts"


def test_google_account_token_columns_are_largebinary_not_null() -> None:
    for name in ("access_token_enc", "refresh_token_enc"):
        col = GoogleAccount.__table__.c[name]
        assert isinstance(col.type, LargeBinary)
        assert col.nullable is False


def test_google_account_token_expires_at_is_required_tz_aware() -> None:
    col = GoogleAccount.__table__.c.token_expires_at
    assert isinstance(col.type, DateTime)
    assert col.type.timezone is True
    assert col.nullable is False


def test_google_account_calendar_id_default_primary() -> None:
    col = GoogleAccount.__table__.c.calendar_id
    assert col.nullable is False
    assert col.server_default.arg.text == "'primary'"


def test_google_account_status_default_active() -> None:
    col = GoogleAccount.__table__.c.status
    assert col.nullable is False
    assert col.server_default.arg.text == "'active'"


def test_google_account_user_fk() -> None:
    fks = list(GoogleAccount.__table__.c.user_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"


def test_google_account_updated_at_has_onupdate() -> None:
    col = GoogleAccount.__table__.c.updated_at
    assert col.nullable is True
    assert col.onupdate is not None


def test_google_account_relationship_to_user() -> None:
    keys = {r.key for r in inspect(GoogleAccount).relationships}
    assert keys == {"user"}


def test_google_account_repr_includes_status() -> None:
    ga = GoogleAccount(
        access_token_enc=b"x",
        refresh_token_enc=b"y",
        token_expires_at=datetime.now(UTC),
        status="active",
    )
    ga.id = uuid.uuid4()
    ga.user_id = uuid.uuid4()
    out = repr(ga)
    assert "GoogleAccount" in out
    assert "active" in out


# ---------- EventReference ----------


def test_event_reference_tablename() -> None:
    assert EventReference.__tablename__ == "event_references"


def test_event_reference_google_event_id_unique_not_null() -> None:
    col = EventReference.__table__.c.google_event_id
    assert isinstance(col.type, String)
    assert col.nullable is False
    assert col.unique is True


def test_event_reference_google_calendar_id_required() -> None:
    col = EventReference.__table__.c.google_calendar_id
    assert col.nullable is False


def test_event_reference_message_fk_is_nullable() -> None:
    col = EventReference.__table__.c.created_from_message_id
    assert col.nullable is True
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "messages"


def test_event_reference_user_fk_required() -> None:
    col = EventReference.__table__.c.user_id
    assert col.nullable is False
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"


def test_event_reference_relationships_declared() -> None:
    keys = {r.key for r in inspect(EventReference).relationships}
    assert keys == {"user", "created_from_message"}


def test_event_reference_repr_includes_event_id() -> None:
    er = EventReference(
        google_calendar_id="primary",
        google_event_id="evt-abc-123",
    )
    er.id = uuid.uuid4()
    er.user_id = uuid.uuid4()
    out = repr(er)
    assert "EventReference" in out
    assert "evt-abc-123" in out


# ---------- Memory ----------


def test_memory_tablename() -> None:
    assert Memory.__tablename__ == "memories"


def test_memory_content_is_required_text() -> None:
    col = Memory.__table__.c.content
    assert isinstance(col.type, Text)
    assert col.nullable is False


def test_memory_tags_is_nullable_string_array() -> None:
    col = Memory.__table__.c.tags
    assert isinstance(col.type, ARRAY)
    assert isinstance(col.type.item_type, String)
    assert col.nullable is True


def test_memory_search_vector_is_tsvector() -> None:
    col = Memory.__table__.c.search_vector
    assert isinstance(col.type, TSVECTOR)
    assert col.nullable is True


def test_memory_source_default_whatsapp() -> None:
    col = Memory.__table__.c.source
    assert col.nullable is False
    assert col.server_default.arg.text == "'whatsapp'"


def test_memory_deleted_at_nullable_for_soft_delete() -> None:
    col = Memory.__table__.c.deleted_at
    assert isinstance(col.type, DateTime)
    assert col.type.timezone is True
    assert col.nullable is True


def test_memory_user_fk() -> None:
    fks = list(Memory.__table__.c.user_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"


def test_memory_gin_indexes_declared() -> None:
    by_name = {idx.name: idx for idx in Memory.__table__.indexes}

    sv_idx = by_name.get("ix_memories_search_vector")
    assert sv_idx is not None
    assert sv_idx.dialect_options["postgresql"]["using"] == "gin"

    tags_idx = by_name.get("ix_memories_tags")
    assert tags_idx is not None
    assert tags_idx.dialect_options["postgresql"]["using"] == "gin"


def test_memory_relationship_to_user() -> None:
    keys = {r.key for r in inspect(Memory).relationships}
    assert keys == {"user"}


def test_memory_repr_includes_source_and_deleted_flag() -> None:
    m = Memory(content="prefers tea over coffee", source="whatsapp")
    m.id = uuid.uuid4()
    m.user_id = uuid.uuid4()
    out = repr(m)
    assert "Memory" in out
    assert "whatsapp" in out
    assert "deleted=False" in out
