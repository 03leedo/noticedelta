"""fk to person: 구독·판정이 프로필 대신 사람을 가리킨다 (platform-services 4.8의 남은 것)

프로필 행은 동의 기록만 남아 있고, 외래 키 때문에 지우지 못했다. 사람(person)을 가리키게 옮긴다.
SQLite는 외래 키를 바꾸려면 표를 다시 만들어야 해서 batch 모드로 한다(이름 없는 외래 키는 이름 규칙으로 찾는다).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-27 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("subscription", "judgment")
CONV = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _swap(table: str, old: tuple[str, str], new: tuple[str, str]) -> None:
    fks = [
        fk
        for fk in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if fk["referred_table"] == old[0] and fk["constrained_columns"] == ["person_id"]
    ]
    with op.batch_alter_table(table, naming_convention=CONV) as batch:
        for fk in fks:
            batch.drop_constraint(
                fk["name"] or f"fk_{table}_person_id_{old[0]}", type_="foreignkey"
            )
        batch.create_foreign_key(f"fk_{table}_person_id_{new[0]}", new[0], ["person_id"], [new[1]])


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        _swap(table, ("profile", "person_id"), ("person", "id"))


def downgrade() -> None:
    """Downgrade schema. 그 사이 지운 프로필이 있으면 빈 동의 기록으로 채운다(예전 외래 키가 가리킬 수 있게)."""
    for table in TABLES:
        op.execute(
            f"INSERT INTO profile (person_id, attributes) SELECT DISTINCT person_id, '{{}}'"
            f" FROM {table} WHERE person_id NOT IN (SELECT person_id FROM profile)"
        )
    for table in TABLES:
        _swap(table, ("person", "id"), ("profile", "person_id"))
