"""session mode: 에이전트 세션의 live / replay (platform-services 5.16)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-27 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_session") as batch:
        batch.add_column(
            sa.Column("mode", sa.String(length=8), nullable=False, server_default="live")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_session") as batch:
        batch.drop_column("mode")
