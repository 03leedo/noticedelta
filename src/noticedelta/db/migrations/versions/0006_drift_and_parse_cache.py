"""drift and parse cache

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "parse_cache",
        sa.Column("blob_key", sa.String(length=64), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("parse_status", sa.String(length=16), nullable=False),
        sa.Column("parser", sa.String(length=64), nullable=True),
        sa.Column("parsed_key", sa.String(length=64), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("segments", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("blob_key", "mime"),
    )
    with op.batch_alter_table("snapshot_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "drift",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("snapshot_run", schema=None) as batch_op:
        batch_op.drop_column("drift")
    op.drop_table("parse_cache")
