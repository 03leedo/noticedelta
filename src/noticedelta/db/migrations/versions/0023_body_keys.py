"""body keys: 본문 조각마다 원본 키 (app-spec 9장, platform-services 8장의 3번)

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-28 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("notice_version") as batch:
        batch.add_column(sa.Column("body_keys", JSON, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notice_version") as batch:
        batch.drop_column("body_keys")
