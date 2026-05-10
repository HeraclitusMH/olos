"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wa_id", sa.String(), nullable=False),
        sa.Column(
            "timezone",
            sa.String(),
            server_default=sa.text("'Europe/Madrid'"),
            nullable=False,
        ),
        sa.Column(
            "locale",
            sa.String(),
            server_default=sa.text("'en'"),
            nullable=False,
        ),
        sa.Column(
            "preferences_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wa_id", name="uq_users_wa_id"),
    )
    op.create_index("ix_users_wa_id", "users", ["wa_id"])

    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wa_message_id", sa.String(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "tool_calls_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "execution_result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "processed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_messages_user_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wa_message_id", name="uq_messages_wa_message_id"),
    )
    # Composite indexes — DESC ordering handled via raw DDL
    op.execute(
        "CREATE INDEX ix_messages_user_id_created_at ON messages (user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_messages_processed_created_at ON messages (processed, created_at)"
    )

    op.create_table(
        "google_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "calendar_id",
            sa.String(),
            server_default=sa.text("'primary'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_google_accounts_user_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "event_references",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("google_calendar_id", sa.String(), nullable=False),
        sa.Column("google_event_id", sa.String(), nullable=False),
        sa.Column(
            "created_from_message_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_event_references_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["created_from_message_id"],
            ["messages.id"],
            name="fk_event_references_message_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_event_id", name="uq_event_references_google_event_id"),
    )

    op.create_table(
        "memories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column(
            "source",
            sa.String(),
            server_default=sa.text("'whatsapp'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_memories_user_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "CREATE INDEX ix_memories_search_vector ON memories USING gin (search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_memories_tags ON memories USING gin (tags)"
    )

    # Trigger: auto-populate search_vector from content + tags on insert/update
    op.execute("""
        CREATE OR REPLACE FUNCTION memories_search_vector_update()
        RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector := to_tsvector(
                'english',
                NEW.content || ' ' || COALESCE(array_to_string(NEW.tags, ' '), '')
            );
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_memories_search_vector
        BEFORE INSERT OR UPDATE ON memories
        FOR EACH ROW EXECUTE FUNCTION memories_search_vector_update();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_memories_search_vector ON memories")
    op.execute("DROP FUNCTION IF EXISTS memories_search_vector_update()")
    op.drop_table("memories")
    op.drop_table("event_references")
    op.drop_table("google_accounts")
    op.drop_table("messages")
    op.drop_table("users")
