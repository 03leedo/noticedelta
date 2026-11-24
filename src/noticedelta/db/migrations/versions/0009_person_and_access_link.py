"""person and access link: 링크 토큰을 해시로만 두고 사람 ID를 따로 둔다

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24 20:00:00.000000

예전에는 개인 링크의 토큰 원문이 곧 사용자 ID(profile.user_id)였고, 구독·판정·알림(멱등 키와
본문 포함)에 그대로 들어갔다. 사람마다 새 ID를 만들고, 토큰은 sha256 해시로 access_link에 두고,
user_id 칸은 person_id로 이름을 바꾼다. 이 마이그레이션이 끝나면 DB에 토큰 원문이 없다.

이름 바꾸기는 ALTER TABLE ... RENAME COLUMN을 쓴다(PostgreSQL, SQLite 3.25+). 표를 새로 만들지
않으므로 다른 표의 외래 키가 그대로 따라온다.
"""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMED = ("profile", "subscription", "judgment", "outbox_alert")
INDEXED = ("subscription", "judgment", "outbox_alert")


def _move_people(conn) -> None:
    """프로필마다 사람을 만들고, 토큰 원문이 있던 칸을 모두 새 ID로 바꾼다."""
    profiles = conn.execute(sa.text("SELECT user_id, consented_at FROM profile")).all()
    at = datetime.now(UTC)
    for token, consented_at in profiles:
        pid = uuid.uuid4().hex
        conn.execute(
            sa.text("INSERT INTO person (id, created_at) VALUES (:id, :at)"),
            {"id": pid, "at": consented_at or at},
        )
        conn.execute(
            sa.text(
                "INSERT INTO access_link (token_hash, person_id, created_at) VALUES (:h, :id, :at)"
            ),
            {"h": hashlib.sha256(token.encode("utf-8")).hexdigest(), "id": pid, "at": at},
        )
        # 자식 행이 부모(profile)를 가리키므로 새 프로필을 먼저 넣고, 자식을 옮기고, 옛 것을 지운다.
        # 외래 키를 끄지 않아도 되고 PostgreSQL·SQLite에서 같게 돈다.
        conn.execute(
            sa.text(
                "INSERT INTO profile (user_id, attributes, consent_version, consented_at,"
                " research_ok) SELECT :id, attributes, consent_version, consented_at, research_ok"
                " FROM profile WHERE user_id = :t"
            ),
            {"id": pid, "t": token},
        )
        for table in ("subscription", "judgment"):
            conn.execute(
                sa.text(f"UPDATE {table} SET user_id = :id WHERE user_id = :t"),
                {"id": pid, "t": token},
            )
        _move_alerts(conn, token, pid)
        conn.execute(sa.text("DELETE FROM profile WHERE user_id = :t"), {"t": token})


def _move_alerts(conn, token: str, pid: str) -> None:
    # 멱등 키는 "사용자:사업:필드:버전"이고 본문(payload)에도 user_id가 있다
    outbox = sa.table(
        "outbox_alert",
        sa.column("id"),
        sa.column("idempotency_key"),
        sa.column("user_id"),
        sa.column("payload", sa.JSON),
    )
    rows = conn.execute(
        sa.select(outbox.c.id, outbox.c.idempotency_key, outbox.c.payload).where(
            outbox.c.user_id == token
        )
    ).all()
    for alert_id, key, payload in rows:
        payload = dict(payload or {})
        if payload.get("user_id") == token:
            payload.pop("user_id")
            payload["person_id"] = pid
        if key.startswith(token + ":"):
            key = pid + key[len(token) :]
        conn.execute(
            outbox.update()
            .where(outbox.c.id == alert_id)
            .values(user_id=pid, idempotency_key=key, payload=payload)
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "person",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "access_link",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"]),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_access_link_person_id", "access_link", ["person_id"], unique=False)

    _move_people(op.get_bind())

    for table in RENAMED:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN user_id TO person_id")
    for table in INDEXED:
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.create_index(f"ix_{table}_person_id", table, ["person_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema. 토큰 원문은 되살릴 수 없으므로 링크는 끊긴다(사람 ID가 user_id로 남는다)."""
    for table in INDEXED:
        op.drop_index(f"ix_{table}_person_id", table_name=table)
        op.create_index(f"ix_{table}_user_id", table, ["person_id"], unique=False)
    for table in RENAMED:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN person_id TO user_id")
    op.drop_index("ix_access_link_person_id", table_name="access_link")
    op.drop_table("access_link")
    op.drop_table("person")
