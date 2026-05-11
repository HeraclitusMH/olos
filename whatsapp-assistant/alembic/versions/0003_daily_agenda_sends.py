"""daily agenda sends

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_agenda_sends",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agenda_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("scheduled_time_local", sa.String(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_daily_agenda_sends_user_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "agenda_date", name="uq_daily_agenda_user_date"
        ),
    )
    op.execute(
        "CREATE INDEX ix_daily_agenda_sends_user_id_agenda_date "
        "ON daily_agenda_sends (user_id, agenda_date DESC)"
    )


def downgrade() -> None:
    op.drop_table("daily_agenda_sends")
