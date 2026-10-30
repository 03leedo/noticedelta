"""마이그레이션이 모델과 같은 스키마를 만드는지, 예전 개발 DB를 이어받는지."""

import os

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from noticedelta.db import make_engine, migrate, models


def diffs(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, models.Base.metadata)


def test_upgrade_builds_the_model_schema(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    assert migrate.upgrade(engine) == migrate.head()
    assert diffs(engine) == []  # 모델을 고치고 마이그레이션을 안 만들면 여기서 깨진다


def test_old_create_all_database_is_stamped_and_kept(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO source (id, name) VALUES ('bizinfo', '기업마당')"))
    assert migrate.current(engine) is None
    assert migrate.upgrade(engine) == migrate.head()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT name FROM source")).scalar() == "기업마당"


def test_upgrade_twice_is_a_no_op(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    migrate.upgrade(engine)
    assert migrate.upgrade(engine) == migrate.head()


PG_URL = os.environ.get("NOTICEDELTA_PG_URL")


@pytest.mark.skipif(not PG_URL, reason="NOTICEDELTA_PG_URL 없음")
def test_postgres_upgrade_matches_models():
    schema = "test_migrations"
    with sa.create_engine(PG_URL).begin() as conn:
        conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}"))
    engine = make_engine(PG_URL, connect_args={"options": f"-csearch_path={schema}"})
    assert migrate.upgrade(engine) == migrate.head()
    assert diffs(engine) == []
    engine.dispose()


def test_database_from_before_the_label_table_gets_it(tmp_path):
    """0001 시절 create_all로 만든 DB(라벨 표 없음)는 0001로 표시한 뒤 0002를 올린다."""
    engine = make_engine(f"sqlite:///{tmp_path / 'v1.db'}")
    old = [t for name, t in models.Base.metadata.tables.items() if name != "label"]
    models.Base.metadata.create_all(engine, tables=old)
    assert migrate.upgrade(engine) == migrate.head()
    assert "label" in sa.inspect(engine).get_table_names()
    assert diffs(engine) == []
