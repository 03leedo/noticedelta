"""flight recorder: 비행 기록기의 묶음과 기록 (platform-services 3장)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "trace",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("parent_id", sa.String(length=26), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("platform_rev", sa.String(length=64), nullable=False),
        sa.Column("app", sa.String(length=80), nullable=True),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("config", JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=True),
        sa.Column("events", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=26), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ref", sa.String(length=200), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=True),
        sa.Column("cost", sa.Integer(), nullable=True),
        sa.Column("payload", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "seq"),
    )
    op.create_index("ix_record_trace_id", "record", ["trace_id"], unique=False)
    op.create_index("ix_record_ref", "record", ["ref"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_record_ref", table_name="record")
    op.drop_index("ix_record_trace_id", table_name="record")
    op.drop_table("record")
    op.drop_table("trace")
