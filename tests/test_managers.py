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
        for org in ("org", "org2"):  # 운영자가 기관을 확인하고 관리자로 둔다
            managers.grant(s, APP.id, ids[org], label=f"{org} 기관", at=T0)
        s.commit()
    return factory, cipher, ids, tokens


def test_only_accepted_managers_see_and_edit_consented_keys(env):
    factory, cipher, ids, _ = env
    with factory() as s:
        code = managers.invite(s, APP.id, ids["org"], at=T0)
        with pytest.raises(managers.NotAManager):  # 확인받지 않은 사람은 초대를 못 만든다
            managers.invite(s, APP.id, ids["lee"], at=T0)
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
            managers.accept(s, managers.invite(s, APP.id, ids[org], at=T0), ids["kim"], at=T0)
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
    resp = client.post(f"/p/{org}/manage/invite")
    assert resp.status_code == 200  # 넘겨주지 않고 이 응답에만 보인다
    code = resp.text.split("/invite/")[1].split("<")[0]
    assert "invite=" not in resp.text
    landing = client.get(f"/invite/{code}").text
    assert "org 기관" in landing and "운영자가 확인한" in landing
    opened = client.post(f"/invite/{code}", data={"link": f"https://x.kr/p/{kim}?group=near#top"})
    assert opened.headers["location"] == f"/p/{kim}/invite/{code}"  # 쿼리·조각은 뗀다
    assert client.post(f"/invite/{code}", data={"link": "https://x.kr/p/nope"}).status_code == 400
    assert client.post(f"/p/{tokens['lee']}/manage/invite").status_code == 403  # 확인받지 않음
    assert client.post(f"/p/{kim}/invite/{code}").status_code == 303  # 받아들인다
    page = client.get(f"/p/{org}/manage").text
    assert ids["kim"][:8] in page and "해당" in page  # 맡은 사람의 자격 요약
    assert ids["lee"][:8] not in page
    mine = client.get(f"/p/{kim}/managers").text
    assert "org 기관" in mine
    client.post(f"/p/{kim}/managers/{APP.id}/{ids['org']}/revoke")
    assert ids["kim"][:8] not in client.get(f"/p/{org}/manage").text
    assert client.get("/invite/nope").status_code == 404


def test_deleting_my_data_closes_manager_links(web):
    client, (factory, _, ids, tokens) = web
    org, kim = tokens["org"], tokens["kim"]
    code = client.post(f"/p/{org}/manage/invite").text.split("/invite/")[1].split("<")[0]
    client.post(f"/p/{kim}/invite/{code}")
    assert client.post(f"/p/{kim}/delete").status_code == 303
    page = client.get(f"/p/{org}/manage")
    assert page.status_code == 200 and ids["kim"][:8] not in page.text
    with factory() as s:
        assert managers.managed(s, ids["org"]) == []


def test_a_later_manager_does_not_widen_what_an_earlier_one_sees(env):
    factory, cipher, ids, _ = env
    with factory() as s:
        managers.accept(s, managers.invite(s, APP.id, ids["org"], at=T0), ids["kim"], at=T0)
        vault.put(
            s, cipher, ids["kim"], {"revenue_krw": 300}, source="input", now=T0
        )  # 뒤에 더 넣음
        managers.accept(s, managers.invite(s, APP.id, ids["org2"], at=T0), ids["kim"], at=T0)
        assert "revenue_krw" not in managers.read(s, cipher, APP.id, ids["org"], ids["kim"], now=T0)
        assert "revenue_krw" in managers.read(s, cipher, APP.id, ids["org2"], ids["kim"], now=T0)
        # 구독자가 지운 값은 관리자가 되살리지 못한다
        vault.put(s, cipher, ids["kim"], {"revenue_krw": None}, source="input", now=T0)
        with pytest.raises(vault.OutsideConsent):
            managers.write(s, cipher, APP.id, ids["org2"], ids["kim"], {"revenue_krw": 1}, now=T0)
        kinds = [e.type for e in s.scalars(sa.select(models.EventRow))]
        assert kinds.count("consent.granted") == 2  # 동의를 준 일이 봉투로 남는다


def test_a_manager_deleting_their_data_withdraws_the_consents_left_behind(env):
    factory, cipher, ids, _ = env
    with factory() as s:
        managers.accept(s, managers.invite(s, APP.id, ids["org"], at=T0), ids["kim"], at=T0)
        managers.forget(s, ids["org"], at=T0)  # 관리자가 자기 정보를 지웠다
        assert vault.active_consent(s, ids["kim"], APP.id, "manage") is None
        assert "consent.withdrawn" in [e.type for e in s.scalars(sa.select(models.EventRow))]
