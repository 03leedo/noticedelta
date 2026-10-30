"""운영 지표 (작업 목록 20번). 실제 흐름(수집 → 알림)을 돌린 뒤 지표를 본다."""

from datetime import UTC, datetime, timedelta

import pytest
from test_history import DOCS, RULES, pdf
from test_pipeline import Env, RulesFromText, subscribe_eligible_user

from noticedelta.metrics import collect, percentile, prometheus_text


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    return env


def test_collect_reports_queues_timings_and_latency(env):
    with env.factory() as s:
        m = collect(s, now=env.clock() + timedelta(minutes=1), window=timedelta(days=3650))
    assert m["jobs"]["parse"] == {"done": 2} and m["queue_depth"]["parse"] == 0
    assert m["run_seconds"]["diff"]["n"] == 2  # 꺼낸 시각과 끝난 시각을 남긴다
    assert m["parse"] == {"ok": 2} and m["parse_failure_rate"] == 0
    assert m["outbox"] == {"sent": 1}
    assert m["correction_to_alert_seconds"]["n"] == 1
    assert m["change_events"] == 2


def test_prometheus_text(env):
    with env.factory() as s:
        text = prometheus_text(collect(s, now=env.clock(), window=timedelta(days=3650)))
    assert 'noticedelta_jobs{kind="parse",status="done"} 2' in text
    assert 'noticedelta_alerts{status="sent"} 1' in text
    assert "noticedelta_change_events 2" in text
    assert text.endswith("\n")


def test_metrics_endpoint_needs_the_token(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from noticedelta.web.app import create_app

    closed = TestClient(create_app(env.factory))
    assert closed.get("/metrics").status_code == 404
    client = TestClient(create_app(env.factory, metrics_token="s3cret"))
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 404
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and "noticedelta_jobs" in ok.text


def test_percentile_nearest_rank():
    xs = [float(i) for i in range(1, 101)]
    assert percentile(xs, 0.5) == 50 and percentile(xs, 0.95) == 95 and percentile(xs, 0.99) == 99
    assert percentile([], 0.5) is None
    assert percentile([3.0], 0.99) == 3.0


def test_empty_database(tmp_path):
    env = Env(tmp_path)
    with env.factory() as s:
        m = collect(s, now=datetime(2026, 11, 1, tzinfo=UTC))
    assert m["jobs"] == {} and m["parse_failure_rate"] is None
    assert m["correction_to_alert_seconds"]["p50"] is None
