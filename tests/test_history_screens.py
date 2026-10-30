"""공고 상세·변경 이력·알림 근거 화면 (작업 목록 16번). 실제 흐름(수집 → 알림)을 돌린 뒤 본다."""

import pytest

pytest.importorskip("fastapi")

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_history import DOCS, RULES, pdf  # noqa: E402
from test_pipeline import Env, RulesFromText, subscribe_eligible_user  # noqa: E402

from noticedelta.db import models  # noqa: E402
from noticedelta.web.app import create_app  # noqa: E402


@pytest.fixture
def flow(tmp_path):
    env = Env(tmp_path)
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        program_id = s.scalar(sa.select(models.Program.id))
        alert_id = s.scalar(sa.select(models.OutboxAlert.id))
    return TestClient(create_app(env.factory)), env, program_id, alert_id


def test_program_page_replays_the_correction(flow):
    client, _, program_id, _ = flow
    page = client.get(f"/programs/{program_id}").text
    assert "v1 → v2" in page and "수정 · 공고문.pdf" in page
    assert "창업 7년 이내" in page and "창업 5년 이내" in page  # 이전과 이후 문장
    assert "최종 확인은 반드시 공고 원문" in page
    assert client.get("/programs/nope").status_code == 404


def test_alert_evidence_follows_the_chain_back_to_the_text(flow):
    client, _, _, alert_id = flow
    inbox = client.get("/p/u1/alerts").text
    assert f"/p/u1/alerts/{alert_id}" in inbox
    page = client.get(f"/p/u1/alerts/{alert_id}").text
    assert "왜 이 알림을 받았나요" in page
    assert "“○ 신청 자격: 공고일 기준 창업 5년 이내" in page  # 근거가 된 새 문장
    assert "창업 7년 이내" in page  # 이번 정정에서 바뀐 이전 문장


def test_someone_elses_alert_is_not_found(flow):
    client, env, _, alert_id = flow
    with env.factory() as s:
        s.add(models.Profile(user_id="u2", attributes={}))
        s.commit()
    assert client.get(f"/p/u2/alerts/{alert_id}").status_code == 404
