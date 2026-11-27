"""관리자 (app-spec 6장): 구독자가 받아들인 관리자만, 동의한 키만 대신 보고 고친다."""

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta import managers, people, vault
from noticedelta.db import make_engine, models
from noticedelta.grants import APP

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    cipher = vault.Cipher(vault.Cipher.new_key())
    with factory() as s:
        ids, tokens = {}, {}
        for name in ("org", "org2", "kim", "lee"):
            p, tok = people.create(s, at=T0)
            s.add(models.Profile(person_id=p.id, attributes={}))
            ids[name], tokens[name] = p.id, tok
        vault.put(s, cipher, ids["kim"], {"region_sido": "부산", "founded_on": "2021-01-01"},
                  source="input", now=T0)  # fmt: skip
        s.commit()
    return factory, cipher, ids, tokens


def test_only_accepted_managers_see_and_edit_consented_keys(env):
    factory, cipher, ids, _ = env
    with factory() as s:
        code = managers.invite(s, APP.id, ids["org"], label="부산창업지원단", at=T0)
        with pytest.raises(managers.NotLinked):  # 받아들이기 전에는 볼 수 없다
            managers.read(s, cipher, APP.id, ids["org"], ids["kim"], now=T0)
        managers.accept(s, code, ids["kim"], at=T0)
        assert managers.read(s, cipher, APP.id, ids["org"], ids["kim"], now=T0) == {
            "region_sido": "부산", "founded_on": "2021-01-01"}  # fmt: skip
        access = s.scalars(sa.select(models.VaultAccessRow)).all()
        assert access[-1].holder == f"manager:{ids['org']}" and access[-1].purpose == "manage"
        managers.write(s, cipher, APP.id, ids["org"], ids["kim"], {"region_sido": "서울"}, now=T0)
        with pytest.raises(vault.OutsideConsent):  # 동의하지 않은 키는 고칠 수 없다
            managers.write(s, cipher, APP.id, ids["org"], ids["kim"], {"revenue_krw": 1}, now=T0)
        with pytest.raises(managers.NotLinked):  # 다른 사람은 볼 수 없다
            managers.read(s, cipher, APP.id, ids["org"], ids["lee"], now=T0)
        with pytest.raises(ValueError):  # 자기 자신은 안 된다
            managers.accept(s, code, ids["org"], at=T0)
        with pytest.raises(ValueError):  # 기한이 지난 초대
            managers.accept(s, code, ids["lee"], at=T0 + timedelta(days=8))


def test_revoking_one_manager_keeps_the_other(env):
    factory, cipher, ids, _ = env
    with factory() as s:
        for org in ("org", "org2"):
            managers.accept(s, managers.invite(s, APP.id, ids[org], label=org, at=T0), ids["kim"],
                            at=T0)  # fmt: skip
        managers.revoke(s, APP.id, ids["org"], ids["kim"], at=T0)
        with pytest.raises(managers.NotLinked):
            managers.read(s, cipher, APP.id, ids["org"], ids["kim"], now=T0)
        assert managers.read(s, cipher, APP.id, ids["org2"], ids["kim"], now=T0)  # 남은 관리자
        managers.revoke(s, APP.id, ids["org2"], ids["kim"], at=T0)
        assert vault.active_consent(s, ids["kim"], APP.id, "manage") is None  # 다 끊으면 동의도


@pytest.fixture
def web(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from noticedelta.web.app import create_app
    from noticedelta.web.demo import seed_demo

    factory, cipher, ids, tokens = env
    with factory() as s:
        seed_demo(s, date(2026, 11, 27))
        s.commit()
    app = create_app(factory, today=lambda: date(2026, 11, 27), now=lambda: T0, cipher=cipher,
                     preview=lambda d, o: {})  # fmt: skip
    return TestClient(app, follow_redirects=False), env


def test_invite_accept_view_and_revoke_on_the_web(web):
    client, (factory, _, ids, tokens) = web
    org, kim = tokens["org"], tokens["kim"]
    resp = client.post(f"/p/{org}/manage/invite", data={"label": "부산창업지원단"})
    code = resp.headers["location"].split("invite=")[1]
    assert f"/invite/{code}" in client.get(resp.headers["location"]).text
    landing = client.get(f"/invite/{code}").text
    assert "부산창업지원단" in landing and "내 링크 주소를 붙여 넣으세요" in landing
    opened = client.post(f"/invite/{code}", data={"link": f"https://x.kr/p/{kim}"})
    assert opened.headers["location"] == f"/p/{kim}/invite/{code}"
    assert client.post(f"/p/{kim}/invite/{code}").status_code == 303  # 받아들인다
    page = client.get(f"/p/{org}/manage").text
    assert ids["kim"][:8] in page and "해당" in page  # 맡은 사람의 자격 요약
    assert ids["lee"][:8] not in page
    mine = client.get(f"/p/{kim}/managers").text
    assert "부산창업지원단" in mine
    client.post(f"/p/{kim}/managers/{APP.id}/{ids['org']}/revoke")
    assert ids["kim"][:8] not in client.get(f"/p/{org}/manage").text
    assert client.get("/invite/nope").status_code == 404


def test_deleting_my_data_closes_manager_links(web):
    client, (factory, _, ids, tokens) = web
    org, kim = tokens["org"], tokens["kim"]
    code = client.post(f"/p/{org}/manage/invite", data={"label": "기관"}).headers["location"]
    code = code.split("invite=")[1]
    client.post(f"/p/{kim}/invite/{code}")
    assert client.post(f"/p/{kim}/delete").status_code == 303
    page = client.get(f"/p/{org}/manage")
    assert page.status_code == 200 and ids["kim"][:8] not in page.text
    with factory() as s:
        assert managers.managed(s, ids["org"]) == []
