"""마이그레이션이 모델과 같은 스키마를 만드는지, 예전 개발 DB를 이어받는지."""

import json
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


def test_unversioned_database_from_the_first_schema_is_upgraded(tmp_path):
    """Alembic 이전에 create_all로 만든 DB(0001 스키마, 버전 표시 없음)는 0001로 표시하고 올린다."""
    from alembic import command

    url = f"sqlite:///{tmp_path / 'v1.db'}"
    command.upgrade(migrate.config(url), migrate.BASELINE)
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE alembic_version"))
    assert migrate.upgrade(engine) == migrate.head()
    assert "label" in sa.inspect(engine).get_table_names()
    assert diffs(engine) == []


def test_link_tokens_leave_the_database_when_people_get_ids(tmp_path):
    """0009: 토큰 원문이 사용자 ID였던 DB를 올리면 토큰은 해시로만 남고, 같은 링크로 들어갈 수 있다."""
    from alembic import command
    from sqlalchemy.orm import Session

    from noticedelta import people

    token = "tok-" + "x" * 20
    url = f"sqlite:///{tmp_path / 'p.db'}"
    command.upgrade(migrate.config(url), "0008")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO program (id, title, title_key, created_at) VALUES ('p1', 't', 't', '2026-11-01')"
            )
        )
        conn.execute(
            sa.text("INSERT INTO profile (user_id, attributes, research_ok) VALUES (:t, '{}', 0)"),
            {"t": token},
        )
        conn.execute(
            sa.text(
                "INSERT INTO subscription (id, user_id, program_id, fields) VALUES ('s1', :t, 'p1', '[]')"
            ),
            {"t": token},
        )
        conn.execute(
            sa.text(
                "INSERT INTO outbox_alert (id, idempotency_key, user_id, program_id, payload, status,"
                " created_at) VALUES ('a1', :k, :t, 'p1', :p, 'sent', '2026-11-01')"
            ),
            {
                "k": f"{token}:p1:deadline:v2",
                "t": token,
                "p": f'{{"user_id": "{token}", "field": "deadline"}}',
            },
        )
    command.upgrade(migrate.config(url), "0009")

    with engine.connect() as conn:
        dump = "\n".join(conn.connection.driver_connection.iterdump())
        sub = conn.execute(sa.text("SELECT person_id FROM subscription")).one()
        alert = conn.execute(
            sa.text("SELECT person_id, idempotency_key, payload FROM outbox_alert")
        ).one()
    assert token not in dump
    with Session(engine) as s:
        pid = people.resolve(s, token)
        assert pid is not None and len(pid) == 32
        assert s.scalar(sa.text("SELECT count(*) FROM profile WHERE person_id = :p"), {"p": pid})
    assert sub.person_id == pid
    assert alert.person_id == pid
    assert alert.idempotency_key == f"{pid}:p1:deadline:v2"
    assert json.loads(alert.payload) == {"person_id": pid, "field": "deadline"}

    # 0010: 결과에 앱과 판이 붙고 멱등 키 앞에 앱 ID가 붙는다
    assert migrate.upgrade(engine) == migrate.head()
    assert diffs(engine) == []
    with Session(engine) as s:
        row = s.scalars(sa.select(models.OutboxAlert)).one()
        sub_app = s.scalars(sa.select(models.SubscriptionRow)).one().app_id
    assert row.idempotency_key == f"grant-alerts:{pid}:p1:deadline:v2"
    assert (row.app_id, row.app_version, sub_app) == ("grant-alerts", 1, "grant-alerts")


def test_subscriptions_and_judgments_move_from_profile_to_person(tmp_path):
    """0019: 외래 키를 사람으로 옮겨도 행이 남고, 프로필을 지워도 구독이 깨지지 않는다. 되돌릴 수도 있다."""
    from alembic import command

    url = f"sqlite:///{tmp_path / 'fk.db'}"
    command.upgrade(migrate.config(url), "0018")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO person (id, created_at) VALUES ('p1', '2026-11-01')"))
        conn.execute(sa.text("INSERT INTO profile (person_id, attributes) VALUES ('p1', '{}')"))
        conn.execute(sa.text("INSERT INTO source (id, name) VALUES ('s', 's')"))
        conn.execute(sa.text(
            "INSERT INTO item (id, title, title_key, created_at) VALUES ('i1', 't', 't', '2026-11-01')"
        ))  # fmt: skip
        conn.execute(sa.text(
            "INSERT INTO subscription (id, person_id, item_id, fields, app_id, app_version)"
            " VALUES ('s1', 'p1', 'i1', '[]', 'grant-alerts', 1)"
        ))  # fmt: skip
    command.upgrade(migrate.config(url), "head")
    fks = sa.inspect(engine).get_foreign_keys("subscription")
    assert {fk["referred_table"] for fk in fks} >= {"person", "item"}
    assert "profile" not in {fk["referred_table"] for fk in fks}
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT person_id FROM subscription")).scalar() == "p1"
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(sa.text("DELETE FROM profile"))  # 프로필은 이제 동의 기록일 뿐이다
    assert diffs(engine) == []
    command.downgrade(migrate.config(url), "0018")
    fks = sa.inspect(make_engine(url)).get_foreign_keys("judgment")
    assert "profile" in {fk["referred_table"] for fk in fks}
