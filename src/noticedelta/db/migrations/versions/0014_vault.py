"""vault: 개인정보 금고의 값·동의·허가·접근 기록 (platform-services 4장)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-25 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "vault_value",
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("person_id", "key"),
    )
    op.create_table(
        "consent",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("keys", JSON, nullable=False),
        sa.Column("text_version", sa.String(length=32), nullable=True),
        sa.Column("granted_at", TS, nullable=False),
        sa.Column("withdrawn_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_consent_person_id", "consent", ["person_id"], unique=False)
    op.create_table(
        "vault_grant",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("keys", JSON, nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("uses_left", sa.Integer(), nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vault_grant_person_id", "vault_grant", ["person_id"], unique=False)
    op.create_table(
        "vault_access",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("grant_id", sa.String(length=32), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("keys", JSON, nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vault_access_grant_id", "vault_access", ["grant_id"], unique=False)
    op.create_index("ix_vault_access_person_id", "vault_access", ["person_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_vault_access_person_id", table_name="vault_access")
    op.drop_index("ix_vault_access_grant_id", table_name="vault_access")
    op.drop_table("vault_access")
    op.drop_index("ix_vault_grant_person_id", table_name="vault_grant")
    op.drop_table("vault_grant")
    op.drop_index("ix_consent_person_id", table_name="consent")
    op.drop_table("consent")
    op.drop_table("vault_value")
