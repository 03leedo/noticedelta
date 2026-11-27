"""검증 화면(내 앱, 판 두 개를 나란히)과 앱이 고른 도구만 쓰기 (app-spec 10장, platform-services 5.2)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import sessionmaker

from noticedelta import app_registry, people, vault
from noticedelta.agent import build_gateway
from noticedelta.appspec import AppDefinition
from noticedelta.db import make_engine, models
from noticedelta.grants import APP

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from noticedelta.web.app import changed_paths, create_app  # noqa: E402

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)


def definition(version, **change):
    data = APP.model_dump(mode="json", by_alias=True, exclude_none=True)
    data.update(id="my-watch", version=version, owner="person:x", **change)
    return AppDefinition.model_validate(data)


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    cipher = vault.Cipher(vault.Cipher.new_key())
    with factory() as s:
        owner, token = people.create(s, at=T0)
        other, other_token = people.create(s, at=T0)
        for p in (owner, other):
            s.add(models.Profile(person_id=p.id, attributes={}))
        app_registry.register(s, definition(1), at=T0)
        app_registry.enable(s, "my-watch", 1, at=T0)
        app_registry.register(s, definition(2, title="(가상) 고친 지킴이"), at=T0)
        app_registry.add_member(s, "my-watch", owner.id, "owner", at=T0)
        s.commit()
        oid = owner.id
    runs = []

    def preview(draft, on):
        runs.append((draft.version, on.version))
        return {"summary": {"알림": 7}, "against_summary": {"알림": 5}, "missing_cassettes": 1,
                "against_missing": 0, "diff": {"new": 3, "gone": 1, "changed": 0,
                                               "broken_tests": 1, "fake_new": 1, "right_new": 2}}  # fmt: skip

    app = create_app(factory, today=lambda: date(2026, 11, 27), now=lambda: T0, cipher=cipher,
                     preview=preview)  # fmt: skip
    return TestClient(app, follow_redirects=False), token, other_token, runs, factory, oid


def test_owner_sees_apps_and_two_versions_side_by_side(env):
    client, token, other_token, runs, _, _ = env
    page = client.get(f"/p/{token}/apps").text
    assert "my-watch" in page and "켠 판 1" in page and "판 2개" in page
    cmp = client.get(f"/p/{token}/apps/my-watch").text  # 기본: 켠 판 대 가장 새 판
    assert runs == [(2, 1)]
    assert "<code>title</code>" in cmp and "고친 지킴이" in cmp  # 무엇을 고쳤는지
    assert "새로 생긴 알림 3개" in cmp and "깨진 시험" in cmp
    assert client.get(f"/p/{other_token}/apps/my-watch").status_code == 404  # 남의 앱
    assert "내가 만든 앱이 없습니다" in client.get(f"/p/{other_token}/apps").text


def test_changed_paths_lists_what_differs():
    a = {"version": 1, "alerts": {"rules": [1]}, "title": "a"}
    b = {"version": 2, "alerts": {"rules": [1, 2]}, "title": "a", "agent": {"tools": ["x"]}}
    assert changed_paths(a, b) == [("agent", None, {"tools": ["x"]}), ("alerts.rules", [1], [1, 2])]


def test_an_app_that_picks_tools_gets_only_those(env):
    _, _, _, _, factory, oid = env
    with factory() as s:
        app_registry.register(s, definition(3, agent={"tools": ["search_items", "check"]}), at=T0)
        app_registry.enable(s, "my-watch", 3, at=T0)
        s.commit()
    gate = build_gateway(factory, preview=lambda d, o: {})
    with factory() as s:
        sid = gate.open_session(s, app_id="my-watch", agent="a", person_id=oid, now=T0).id
        mine = gate.open_session(s, app_id=APP.id, agent="a", person_id=oid, now=T0).id
        s.commit()
        assert "blocked" in gate.call(s, sid, "send_inquiry",
                                      {"item_id": "i", "to": "a@b.kr", "kind": "other"}, now=T0)  # fmt: skip
        assert "blocked" not in gate.call(s, sid, "search_items", {"query": "x"}, now=T0)
        # 지원사업 앱은 고르지 않았으니 모두 쓴다
        assert "blocked" not in gate.call(s, mine, "app_schema", {}, now=T0)
        bad = definition(4, agent={"tools": ["rm_rf"]}).model_dump(mode="json", by_alias=True)
        out = gate.call(s, mine, "check_app", {"definition": bad}, now=T0)
        assert not out["ok"] and any("rm_rf" in p["what"] for p in out["problems"])
