"""판정 화면 (작업 목록 7번). 가상 사업 셋으로 화면·근거·예정일·캘린더를 확인한다."""

from datetime import date

import pytest

pytest.importorskip("fastapi")

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from noticedelta.db import make_engine, models  # noqa: E402
from noticedelta.web.app import attributes_from_form, create_app  # noqa: E402
from noticedelta.web.demo import seed_demo  # noqa: E402
from noticedelta.web.views import humanize  # noqa: E402

TODAY = date(2026, 10, 30)
YOUTH = "(가상) 2026 부산 청년 창업 지원사업"


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'w.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        seed_demo(s, TODAY)
        s.commit()
    client = TestClient(create_app(factory, today=lambda: TODAY), follow_redirects=False)
    return client, factory


def make_profile(client, **form):
    resp = client.post("/profiles", data=form)
    assert resp.status_code == 303
    return resp.headers["location"]


BUSAN_FOUNDER = {
    "founded_on": "2021-01-01",
    "ceo_birth_date": "1993-05-01",
    "region_sido": "부산",
    "revenue_krw": "300,000,000",
}


def test_home_shows_the_profile_form(env):
    client, _ = env
    resp = client.get("/")
    assert resp.status_code == 200 and "내가 받을 수 있는 지원사업" in resp.text


def test_profile_gets_an_unguessable_personal_link(env):
    client, factory = env
    path = make_profile(client, **BUSAN_FOUNDER)
    token = path.rsplit("/", 1)[-1]
    assert path.startswith("/p/") and len(token) >= 24
    with factory() as s:
        attrs = s.get(models.Profile, token).attributes
    assert attrs["revenue_krw"] == 300_000_000 and attrs["founded_on"] == "2021-01-01"


def test_groups_reasons_and_quotes(env):
    client, _ = env
    path = make_profile(client, **BUSAN_FOUNDER)
    page = client.get(path).text
    assert "해당 1" in page and YOUTH in page
    assert "“○ 신청 자격: 공고일 기준 창업 7년 이내 기업”" in page  # 원문 근거
    assert "업력(개월) ≤ 84" in page  # 읽기 쉬운 근거

    near = client.get(f"{path}/list?group=near").text
    assert "(가상) 초기 창업 바우처" in near and "이 조건 하나만" in near
    assert "<html" not in near  # HTMX가 끼워 넣을 조각만


def test_shows_the_day_eligibility_runs_out(env):
    """업력 84개월을 마감 전에 넘기는 사람에게 그날을 미리 알린다."""
    client, _ = env
    path = make_profile(client, **{**BUSAN_FOUNDER, "founded_on": "2019-10-20"})
    page = client.get(path).text
    assert "2026-11-20부터 판정이 바뀝니다" in page


def test_calendar_has_deadlines_and_change_days(env):
    client, _ = env
    path = make_profile(client, **{**BUSAN_FOUNDER, "founded_on": "2019-10-20"})
    resp = client.get(f"{path}/calendar.ics")
    assert resp.headers["content-type"].startswith("text/calendar")
    body = resp.text
    assert body.startswith("BEGIN:VCALENDAR") and "\r\n" in body
    assert "DTSTART;VALUE=DATE:20261120" in body  # 마감일이자 자격이 바뀌는 날
    assert "[자격 변경]" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_unknown_link_is_404_and_edit_saves(env):
    client, _ = env
    assert client.get("/p/not-a-real-token").status_code == 404
    path = make_profile(client, **BUSAN_FOUNDER)
    resp = client.post(f"{path}/edit", data={**BUSAN_FOUNDER, "region_sido": "서울"})
    assert resp.status_code == 303
    assert "해당 0" in client.get(path).text


def test_titles_are_escaped(env):
    client, factory = env
    with factory() as s:
        s.execute(sa.update(models.Program).values(title="<script>alert(1)</script>"))
        s.commit()
    page = client.get(make_profile(client, **BUSAN_FOUNDER)).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_form_values_are_cleaned():
    attrs = attributes_from_form(
        {"region_sido": "화성", "founded_on": "2026-13-01", "employees": "12", "is_restart": "on"}
    )
    assert attrs == {
        "employees": 12,
        "is_pre_founder": False,
        "is_women_owned": False,
        "is_restart": True,
    }


def test_humanize():
    assert humanize("is_women_owned == True (프로필: False)") == "여성기업 = 예 (프로필: 아니오)"


def test_inbox_shows_sent_alerts(env):
    from datetime import UTC, datetime

    client, factory = env
    path = make_profile(client, **BUSAN_FOUNDER)
    token = path.rsplit("/", 1)[-1]
    assert "아직 받은 알림이 없습니다" in client.get(f"{path}/alerts").text
    with factory() as s:
        s.add(
            models.OutboxAlert(
                idempotency_key=f"{token}:demo-youth:eligibility_rules:pv-youth",
                user_id=token,
                program_id="demo-youth",
                payload={
                    "message": "(가상) 사업: 신청 자격 판정이 해당에서 비해당(으)로 바뀌었습니다."
                },
                status="sent",
                sent_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
            )
        )
        s.commit()
    page = client.get(f"{path}/alerts").text
    assert "해당에서 비해당" in page and "2026-10-30 09:00" in page
    assert client.get("/p/nope/alerts").status_code == 404
