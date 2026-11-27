"""user source: 사용자가 등록한 출처 (source-catalog.md 4장)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_source",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=False),
        sa.Column("body", JSON, nullable=False),
        sa.Column("registered_at", TS, nullable=False),
        sa.Column("removed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_source_owner", "user_source", ["owner"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_user_source_owner", table_name="user_source")
    op.drop_table("user_source")
