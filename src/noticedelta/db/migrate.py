"""DB 스키마를 마이그레이션으로 최신으로 맞춘다 (Alembic).

    uv run alembic upgrade head                                   # .env의 DATABASE_URL
    uv run alembic revision --autogenerate -m "무엇을 바꿨나"      # 모델을 고친 뒤

스크립트는 시작할 때 upgrade(engine)을 부른다. create_all로 만들어 둔 예전 개발 DB는
첫 마이그레이션(0001) 상태로 표시(stamp)한 뒤 그다음부터 올린다.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001"  # create_all 시절 스키마와 같은 첫 마이그레이션


def config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def head() -> str:
    return ScriptDirectory.from_config(config()).get_current_head()


def current(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine: sa.Engine) -> str:
    """최신 리비전까지 올리고 그 리비전을 돌려준다."""
    cfg = config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(sa.inspect(conn).get_table_names())
        version = MigrationContext.configure(conn).get_current_revision()
        if version is None and "program" in tables:
            command.stamp(cfg, BASELINE)  # create_all로 만든 예전 DB
        command.upgrade(cfg, "head")
    return current(engine)
