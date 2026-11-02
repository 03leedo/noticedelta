"""판정 화면 (작업 목록 7번). 가상 사업 셋으로 화면·근거·예정일·캘린더를 확인한다."""

from datetime import date

import pytest

pytest.importorskip("fastapi")

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from noticedelta.db import make_engine, models  # noqa: E402
from noticedelta.web import consent  # noqa: E402
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
    resp = client.post("/profiles", data={"agree": "on", **form})
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


def count_rows(s, model, **where):
    q = sa.select(sa.func.count()).select_from(model)
    for k, v in where.items():
        q = q.where(getattr(model, k) == v)
    return s.scalar(q)


def test_profile_needs_consent_and_keeps_typed_values(env):
    client, factory = env
    with factory() as s:
        before = count_rows(s, models.Profile)
    resp = client.post("/profiles", data=BUSAN_FOUNDER)
    assert resp.status_code == 400 and "동의해야" in resp.text
    assert 'value="2021-01-01"' in resp.text  # 입력한 값은 다시 채워 보여 준다
    with factory() as s:
        assert count_rows(s, models.Profile) == before  # 동의 없이는 아무것도 저장하지 않는다


def test_consent_is_recorded_and_research_can_be_turned_off(env):
    client, factory = env
    path = make_profile(client, research_ok="on", **BUSAN_FOUNDER)
    token = path.rsplit("/", 1)[-1]
    with factory() as s:
        p = s.get(models.Profile, token)
        assert p.consent_version == consent.VERSION and p.consented_at and p.research_ok
    assert "checked" in client.get(f"{path}/edit").text
    client.post(f"{path}/edit", data=BUSAN_FOUNDER)  # 선택 동의 칸을 끄고 저장
    with factory() as s:
        assert s.get(models.Profile, token).research_ok is False


def test_delete_removes_everything_and_the_link_stops_working(env):
    client, factory = env
    path = make_profile(client, **BUSAN_FOUNDER)
    token = path.rsplit("/", 1)[-1]
    with factory() as s:
        s.add(models.SubscriptionRow(user_id=token, program_id="demo-youth", fields=[]))
        s.add(
            models.OutboxAlert(
                idempotency_key=f"{token}:k",
                user_id=token,
                program_id="demo-youth",
                payload={},
                status="sent",
            )
        )
        s.commit()
    assert "모두 지우기" in client.get(f"{path}/delete").text
    resp = client.post(f"{path}/delete")
    assert resp.status_code == 303 and resp.headers["location"] == "/?deleted=1"
    assert client.get(path).status_code == 404
    assert client.post(f"{path}/delete").status_code == 404
    with factory() as s:
        for model in (models.Profile, models.SubscriptionRow, models.OutboxAlert):
            assert count_rows(s, model, user_id=token) == 0
    assert "모두 지웠습니다" in client.get("/?deleted=1").text


def test_privacy_page_shows_items_retention_and_contact(env):
    _, factory = env
    client = TestClient(create_app(factory, contact="베타 문의 창구"))
    page = client.get("/privacy").text
    assert consent.ITEMS in page and consent.KEEP_UNTIL in page and "베타 문의 창구" in page
    assert "/privacy" in client.get("/").text  # 첫 화면에서 안내로 갈 수 있다
