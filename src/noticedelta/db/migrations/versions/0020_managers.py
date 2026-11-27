"""managers: 관리자의 초대와 구독자와의 연결 (app-spec 6장)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-27 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "manage_invite",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("manager_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=50), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_manage_invite_manager_id", "manage_invite", ["manager_id"], unique=False)
    op.create_table(
        "manage_link",
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("manager_id", sa.String(length=32), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=50), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("app_id", "manager_id", "person_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("manage_link")
    op.drop_index("ix_manage_invite_manager_id", table_name="manage_invite")
    op.drop_table("manage_invite")
