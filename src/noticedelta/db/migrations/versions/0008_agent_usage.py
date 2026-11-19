"""agent usage

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session", sa.String(length=32), nullable=False),
        sa.Column("client", sa.String(length=64), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("flagged_lines", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tool_call",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session", sa.String(length=32), nullable=False),
        sa.Column("client", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.String(length=16), nullable=False),
        sa.Column("arg_shape", JSON, nullable=False),
        sa.Column("arg_digest", sa.String(length=16), nullable=False),
        sa.Column("logged", JSON, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("hidden_lines", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tool_call", schema=None) as batch_op:
        batch_op.create_index("ix_tool_call_session_seq", ["session", "seq"], unique=False)
        batch_op.create_index("ix_tool_call_tool_at", ["tool", "at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tool_call", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_call_tool_at")
        batch_op.drop_index("ix_tool_call_session_seq")
    op.drop_table("tool_call")
    op.drop_table("agent_feedback")
