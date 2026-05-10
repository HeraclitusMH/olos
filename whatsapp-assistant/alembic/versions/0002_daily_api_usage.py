"""daily api usage counter

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_api_usage",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column(
            "request_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("usage_date"),
    )


def downgrade() -> None:
    op.drop_table("daily_api_usage")
