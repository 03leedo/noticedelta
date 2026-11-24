"""app definition: 앱 정의를 DB에 두고 결과마다 앱과 판을 단다

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-24 22:00:00.000000

app-spec.md 15장의 "결과에 앱 표시": 필드 값, 판정, 구독, 라벨, 알림에 앱 ID와 판을 단다. 앱이 둘이
되는 순간 섞이지 않게 하기 위해서다. 지금까지의 결과는 모두 첫 앱(지원사업 정정 알림, 판 1)이 만든
것이라 그 값으로 채운다. 알림의 멱등 키에도 앱 ID를 앞에 붙인다(두 앱이 같은 필드 이름을 쓰면
한쪽 알림이 다른 쪽에 막히지 않게).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TABLES = ("field_value", "subscription", "judgment", "label", "outbox_alert")
FIRST_APP, FIRST_VERSION = "grant-alerts", 1  # 이 마이그레이션 전의 결과를 만든 앱


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "app_definition",
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("body", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("app_id", "version"),
    )
    for table in TABLES:
        op.add_column(table, sa.Column("app_id", sa.String(length=64), nullable=True))
        op.add_column(table, sa.Column("app_version", sa.Integer(), nullable=True))
        op.execute(
            sa.text(f"UPDATE {table} SET app_id = :a, app_version = :v").bindparams(
                a=FIRST_APP, v=FIRST_VERSION
            )
        )
    op.execute(
        sa.text("UPDATE outbox_alert SET idempotency_key = :p || idempotency_key").bindparams(
            p=FIRST_APP + ":"
        )
    )
    # 채운 뒤 NOT NULL로. SQLite는 표를 새로 만들어 옮긴다(이 표들은 다른 표가 가리키지 않는다)
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("app_id", existing_type=sa.String(length=64), nullable=False)
            batch.alter_column("app_version", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text(
            "UPDATE outbox_alert SET idempotency_key = substr(idempotency_key, :n)"
            " WHERE idempotency_key LIKE :p"
        ).bindparams(n=len(FIRST_APP) + 2, p=FIRST_APP + ":%")
    )
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("app_version")
            batch.drop_column("app_id")
    op.drop_table("app_definition")
