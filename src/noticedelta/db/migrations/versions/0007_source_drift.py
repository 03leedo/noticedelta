"""source drift

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "source_drift",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_run", sa.String(length=64), nullable=False),
        sa.Column("last_run", sa.String(length=64), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column("changed", sa.Integer(), nullable=False),
        sa.Column("compared", sa.Integer(), nullable=False),
        sa.Column(
            "samples",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("source_drift", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_source_drift_source"), ["source"], unique=False)
        batch_op.create_index(batch_op.f("ix_source_drift_status"), ["status"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("source_drift", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_source_drift_status"))
        batch_op.drop_index(batch_op.f("ix_source_drift_source"))
    op.drop_table("source_drift")
