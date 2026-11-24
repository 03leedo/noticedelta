"""앱 정의의 원본(DB)과 결과에 붙는 앱·판."""

import copy
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta import app_registry, grants
from noticedelta.appspec import AppDefinition
from noticedelta.db import models
from noticedelta.impact import _key

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


def bumped(version: int, guide: str = "") -> AppDefinition:
    data = grants.APP.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data = copy.deepcopy(data) | {"version": version}
    if guide:
        data["engine"]["fields"]["amount"]["guide"] = guide
    return AppDefinition.model_validate(data)


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_seed_registers_and_enables_once(session):
    app_registry.seed(session, grants.APP, at=T0)
    app_registry.seed(session, grants.APP, at=T1)  # 다시 넣어도 그대로
    rows = session.scalars(sa.select(models.AppDefinitionRow)).all()
    assert [(r.app_id, r.version, r.enabled_at) for r in rows] == [
        ("grant-alerts", 1, T0.replace(tzinfo=None))
    ]
    assert app_registry.current(session, "grant-alerts") == grants.APP
    assert rows[0].content_hash == grants.APP.content_hash()


def test_same_version_with_other_content_is_refused(session):
    app_registry.seed(session, grants.APP, at=T0)
    with pytest.raises(app_registry.VersionConflict, match="version을 2로 올린다"):
        app_registry.register(session, bumped(1, guide="바꾼 안내"), at=T1)


def test_a_higher_version_from_the_seed_becomes_current(session):
    app_registry.seed(session, grants.APP, at=T0)
    v2 = bumped(2, guide="바꾼 안내")
    app_registry.seed(session, v2, at=T1)
    assert app_registry.current(session, "grant-alerts").version == 2
    # 낮은 판의 씨앗은 켠 판을 되돌리지 않는다
    app_registry.seed(session, grants.APP, at=T1)
    assert app_registry.current(session, "grant-alerts").version == 2


def test_register_does_not_enable(session):
    app_registry.register(session, bumped(3), at=T0)  # 사람의 확인 전(app-spec 3장)
    assert app_registry.current(session, "grant-alerts") is None
    app_registry.enable(session, "grant-alerts", 3, at=T1)
    assert app_registry.current(session, "grant-alerts").version == 3
    with pytest.raises(KeyError):
        app_registry.enable(session, "grant-alerts", 9, at=T1)


def test_alert_keys_of_two_apps_do_not_collide():
    a = _key("p1", "prog", "deadline", "v2", "grant-alerts")
    b = _key("p1", "prog", "deadline", "v2", "other-app")
    assert a != b and a.startswith("grant-alerts:")
