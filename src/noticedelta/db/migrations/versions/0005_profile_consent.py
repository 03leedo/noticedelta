"""profile consent

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("profile", schema=None) as batch_op:
        batch_op.add_column(sa.Column("consent_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("research_ok", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("profile", schema=None) as batch_op:
        batch_op.drop_column("research_ok")
        batch_op.drop_column("consented_at")
        batch_op.drop_column("consent_version")
