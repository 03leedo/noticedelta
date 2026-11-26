"""tool gateway: 에이전트 세션·효과·판단 기록 (platform-services 5장)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-25 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_session",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=True),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("flags", JSON, nullable=False),
        sa.Column("seen", JSON, nullable=False),
        sa.Column("sources", JSON, nullable=False),
        sa.Column("calls", JSON, nullable=False),
        sa.Column("effects", sa.Integer(), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("closed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_session_owner",
        "agent_session",
        ["person_id", "app_id", "agent", "status"],
        unique=False,
    )
    op.create_table(
        "tool_action",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("session_id", sa.String(length=26), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("effect", sa.String(length=16), nullable=False),
        sa.Column("arguments", JSON, nullable=False),
        sa.Column("dropped", JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("policy", sa.String(length=16), nullable=False),
        sa.Column("marks", JSON, nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("evidence", JSON, nullable=False),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("decide_by", TS, nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", TS, nullable=True),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("commit_after", TS, nullable=True),
        sa.Column("committed_at", TS, nullable=True),
        sa.Column("result", JSON, nullable=True),
        sa.Column("undo", JSON, nullable=True),
        sa.Column("undone_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_action_session_id", "tool_action", ["session_id"], unique=False)
    op.create_index("ix_tool_action_status", "tool_action", ["status"], unique=False)
    op.create_table(
        "tool_decision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=26), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("flags_before", JSON, nullable=False),
        sa.Column("flags_after", JSON, nullable=False),
        sa.Column("action_id", sa.String(length=26), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_decision_session_id", "tool_decision", ["session_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tool_decision_session_id", table_name="tool_decision")
    op.drop_table("tool_decision")
    op.drop_index("ix_tool_action_status", table_name="tool_action")
    op.drop_index("ix_tool_action_session_id", table_name="tool_action")
    op.drop_table("tool_action")
    op.drop_index("ix_agent_session_owner", table_name="agent_session")
    op.drop_table("agent_session")
