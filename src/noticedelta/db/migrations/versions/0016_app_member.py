"""app member: 앱의 소유자·관리자 (app-spec 6장)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-26 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "app_member",
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("added_at", TS, nullable=False),
        sa.Column("removed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("app_id", "person_id", "role"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("app_member")
