"""app budget: 앱별 하루 LLM 토큰 한도 (app-spec 9장)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-27 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "app_budget",
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("daily_tokens", sa.Integer(), nullable=False),
        sa.Column("set_by", sa.String(length=64), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("app_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("app_budget")
