"""앱을 만드는 조종 도구: 에이전트가 만들고, 켜는 순간은 소유자가 확인한다 (app-spec 3·6장)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from noticedelta import app_registry
from noticedelta.agent import build_gateway
from noticedelta.db import make_engine, models
from noticedelta.grants import APP

T0 = datetime(2026, 11, 26, 9, tzinfo=UTC)


def definition(app_id="my-deadlines", version=1):
    data = APP.model_dump(mode="json", by_alias=True, exclude_none=True)
    data.update(id=app_id, version=version, title="(가상) 내 마감 지킴이")
    return data


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'a.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    previews = []

    def preview(draft, on):
        previews.append((draft.id, draft.version, on.version if on else None))
        return {"draft": f"{draft.id}@{draft.version}", "diff": {"new": 0}}

    gate = build_gateway(factory, preview=preview)
    with factory() as s:
        app_registry.seed(s, APP, at=T0)  # 운영자가 넣은 앱(소유자 없음)
        s.commit()
    return factory, gate, previews


def session(factory, gate, person):
    with factory() as s:
        sid = gate.open_session(s, app_id=APP.id, agent="builder", person_id=person, now=T0).id
        s.commit()
    return sid


def call(factory, gate, sid, name, **args):
    with factory() as s:
        return gate.call(s, sid, name, args, now=T0)


def test_an_agent_builds_an_app_and_the_owner_confirms_enabling(env):
    factory, gate, previews = env
    sid = session(factory, gate, "p1")
    assert "properties" in call(factory, gate, sid, "app_schema")["schema"]
    bad = call(factory, gate, sid, "check_app", definition={**definition(), "version": 0})
    assert bad["ok"] is False and bad["problems"]
    assert call(factory, gate, sid, "check_app", definition=definition())["ok"]
    saved = call(factory, gate, sid, "save_app", definition=definition())
    assert (
        saved["ok"] and saved["action"]["status"] == "committed"
    )  # 초안은 바로 넣고 되돌릴 수 있다
    with factory() as s:
        assert app_registry.roles_of(s, "my-deadlines", "p1") == {"owner"}
        assert app_registry.current(s, "my-deadlines") is None  # 넣었을 뿐 켜지 않았다
        assert s.get(models.AppDefinitionRow, ("my-deadlines", 1)).body["owner"] == "person:p1"
    assert call(factory, gate, sid, "preview_app", app_id="my-deadlines", version=1)["diff"]
    assert previews == [("my-deadlines", 1, None)]
    # 켜기는 늘 소유자의 확인을 받는다(속성이 하나도 없는 세션이어도)
    out = call(factory, gate, sid, "enable_app", app_id="my-deadlines", version=1)["action"]
    assert out["status"] == "held" and "확인이 꼭 필요한" in out["reason"]
    with factory() as s:
        [card] = gate.inbox(s, person_id="p1")
        assert card["tool"] == "enable_app"
        gate.approve(s, out["id"], by="person:p1", now=T0)
        s.commit()
        gate.commit_due(s, now=T0 + timedelta(minutes=10))
        assert app_registry.current(s, "my-deadlines").version == 1


def test_only_owners_change_an_app_and_platform_apps_are_off_limits(env):
    factory, gate, _ = env
    owner, other = session(factory, gate, "p1"), session(factory, gate, "p2")
    call(factory, gate, owner, "save_app", definition=definition())
    v2 = call(factory, gate, other, "save_app", definition=definition(version=2))
    assert v2["blocked"]["step"] == "authorize"
    assert "blocked" in call(factory, gate, other, "enable_app", app_id="my-deadlines", version=1)
    assert "blocked" in call(factory, gate, other, "preview_app", app_id="my-deadlines", version=1)
    ours = call(factory, gate, owner, "save_app", definition=definition(APP.id, version=2))
    assert "소유자만" in ours["blocked"]["reason"]  # 운영자가 넣은 앱은 아무도 고칠 수 없다
    anon = session(factory, gate, None)
    assert "사람이 정해지지 않은" in call(factory, gate, anon, "save_app",
                                          definition=definition("x-app"))["blocked"]["reason"]  # fmt: skip
    # 에이전트가 소유자를 사칭할 수 없다
    forged = call(factory, gate, other, "save_app", definition=definition("x2"), actor="person:p1")
    assert "blocked" in forged


def test_saving_a_draft_can_be_undone_and_bad_definitions_are_not_saved(env):
    factory, gate, _ = env
    sid = session(factory, gate, "p1")
    saved = call(factory, gate, sid, "save_app", definition=definition())
    bad = call(factory, gate, sid, "save_app", definition={**definition(version=2), "kind": "x"})
    assert "error" in bad and "check_app" in bad["error"]
    with factory() as s:
        gate.undo(s, saved["action"]["id"], by="person:p1", now=T0)
        s.commit()
        assert s.get(models.AppDefinitionRow, ("my-deadlines", 1)) is None
