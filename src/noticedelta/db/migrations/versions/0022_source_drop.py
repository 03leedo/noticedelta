"""source drop: 파일 올리기 출처에 올린 판 (source-catalog.md 3장)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-27 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "source_drop",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=True),
        sa.Column("uploaded_at", TS, nullable=False),
        sa.Column("uploaded_by", sa.String(length=32), nullable=False),
        sa.Column("withdrawn_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "seq", name="uq_source_drop_seq"),
    )
    op.create_index("ix_source_drop_source_id", "source_drop", ["source_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_source_drop_source_id", table_name="source_drop")
    op.drop_table("source_drop")
