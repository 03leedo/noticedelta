"""program to item: 사업(program)을 항목(item)으로

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-24 23:00:00.000000

app-spec.md 15장의 결정. "사업"은 지원사업 앱의 낱말이고 플랫폼은 어떤 앱의 항목이든 다룬다.
표와 칸의 이름만 바꾸고 값은 그대로다. ALTER TABLE ... RENAME(PostgreSQL, SQLite 3.26+)은 다른 표의
외래 키를 함께 고친다. 인덱스는 이름에 옛 낱말이 들어 있어 지우고 새 이름으로 다시 만든다.
작업 큐에 남은 작업의 본문 키(program_id, program_version_id)와 동시 실행 키(program:…), 알림 본문의
program_id도 새 이름으로 바꾼다.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = {"program": "item", "program_version": "item_version", "program_link": "item_link"}
ITEM_ID = ("notice", "item_link", "item_version", "field_value", "eligibility_rule",
           "subscription", "judgment", "change_event", "label", "outbox_alert")  # fmt: skip
ITEM_VERSION_ID = ("field_value", "eligibility_rule", "judgment")
# (옛 이름, 새 이름, 표(바꾼 뒤), 칸(바꾼 뒤))
INDEXES = [
    ("ix_program_title_key", "ix_item_title_key", "item", "title_key"),
    ("ix_program_link_notice_id", "ix_item_link_notice_id", "item_link", "notice_id"),
    ("ix_program_link_program_id", "ix_item_link_item_id", "item_link", "item_id"),
    ("ix_program_version_program_id", "ix_item_version_item_id", "item_version", "item_id"),
] + [
    (f"ix_{t}_program_id", f"ix_{t}_item_id", t, "item_id")
    for t in (
        "notice",
        "field_value",
        "eligibility_rule",
        "subscription",
        "judgment",
        "change_event",
        "label",
        "outbox_alert",
    )  # fmt: skip
]
PAYLOAD_KEYS = {"program_id": "item_id", "program_version_id": "item_version_id"}


def _rename_keys(d: dict, mapping: dict[str, str]) -> dict:
    return {mapping.get(k, k): v for k, v in d.items()}


def _rewrite_payloads(
    conn, table: str, mapping: dict[str, str], concurrency: tuple[str, str] | None
):
    cols = "id, payload" + (", concurrency_key" if concurrency else "")
    for row in conn.execute(sa.text(f"SELECT {cols} FROM {table}")).all():
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload or "{}")
        new = _rename_keys(payload, mapping)
        values = {"id": row.id, "p": json.dumps(new, ensure_ascii=False)}
        sets = "payload = :p"
        if concurrency and (row.concurrency_key or "").startswith(concurrency[0]):
            values["c"] = concurrency[1] + row.concurrency_key[len(concurrency[0]) :]
            sets += ", concurrency_key = :c"
        elif new == payload:
            continue
        cast = "CAST(:p AS JSONB)" if conn.dialect.name == "postgresql" else ":p"
        conn.execute(
            sa.text(f"UPDATE {table} SET {sets.replace(':p', cast)} WHERE id = :id"), values
        )


def upgrade() -> None:
    """Upgrade schema."""
    for old, new in TABLES.items():
        op.execute(f"ALTER TABLE {old} RENAME TO {new}")
    for table in ITEM_ID:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN program_id TO item_id")
    for table in ITEM_VERSION_ID:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN program_version_id TO item_version_id")
    for old, new, table, column in INDEXES:
        op.drop_index(old, table_name=table)
        op.create_index(new, table, [column], unique=False)
    conn = op.get_bind()
    _rewrite_payloads(conn, "job", PAYLOAD_KEYS, ("program:", "item:"))
    _rewrite_payloads(conn, "outbox_alert", PAYLOAD_KEYS, None)


def downgrade() -> None:
    """Downgrade schema."""
    back = {v: k for k, v in PAYLOAD_KEYS.items()}
    conn = op.get_bind()
    _rewrite_payloads(conn, "job", back, ("item:", "program:"))
    _rewrite_payloads(conn, "outbox_alert", back, None)
    for old, new, table, column in INDEXES:
        op.drop_index(new, table_name=table)
        op.create_index(old, table, [column], unique=False)
    for table in ITEM_VERSION_ID:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN item_version_id TO program_version_id")
    for table in ITEM_ID:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN item_id TO program_id")
    for old, new in TABLES.items():
        op.execute(f"ALTER TABLE {new} RENAME TO {old}")
