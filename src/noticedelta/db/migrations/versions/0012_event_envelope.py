"""event envelope: 이벤트 봉투 (platform-services 2장)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "event",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("v", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("app", sa.String(length=80), nullable=True),
        sa.Column("subject", sa.String(length=64), nullable=True),
        sa.Column("item", sa.String(length=40), nullable=True),
        sa.Column("item_version", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(length=26), nullable=True),
        sa.Column("causation_id", sa.String(length=26), nullable=True),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("labels", JSON, nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("type", "idempotency_key"),
    )
    op.create_index("ix_event_type", "event", ["type"], unique=False)
    op.create_index("ix_event_item", "event", ["item"], unique=False)
    op.create_index("ix_event_trace_id", "event", ["trace_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_event_trace_id", table_name="event")
    op.drop_index("ix_event_item", table_name="event")
    op.drop_index("ix_event_type", table_name="event")
    op.drop_table("event")
