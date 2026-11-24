"""이벤트 봉투 (platform-services 2장): 모양, 등록부, 한 번만, 구독 → 작업 큐, 추적 사슬."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from pydantic import ValidationError
from sqlalchemy.orm import Session
from test_history import DOCS, RULES, pdf
from test_pipeline import Env, RulesFromText, subscribe_eligible_user

from noticedelta import events
from noticedelta.db import models
from noticedelta.events import Ref, Subscription, Trace, emit, new_ulid

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_ulid_sorts_by_time_even_within_a_millisecond():
    a, b = new_ulid(T0 + timedelta(days=9000)), new_ulid(T0 + timedelta(days=9000, milliseconds=1))
    assert len(a) == 26 and a < b
    same = [new_ulid() for _ in range(50)]
    assert same == sorted(same) and len(set(same)) == 50


def test_unknown_type_and_extra_payload_are_refused(session):
    with pytest.raises(KeyError, match="등록되지 않은"):
        emit(session, "item.exploded", {}, producer="svc:x", key="k")
    with pytest.raises(ValidationError):  # 정해진 칸 밖(예: 개인정보 값)은 받지 않는다
        emit(
            session,
            "verdict.changed",
            {
                "field": "f",
                "to_version_id": "v",
                "before": "eligible",
                "after": "ineligible",
                "evidence": [],
                "attributes": {"birth": "1990-01-01"},
            },
            producer="svc:engine",
            key="k",
        )


def test_same_key_is_written_once(session):
    body = {"app_id": "a", "version": 1, "content_hash": "h"}
    first = emit(session, "app.version_enabled", body, producer="svc:apps", key="a@1")
    again = emit(session, "app.version_enabled", body, producer="svc:apps", key="a@1")
    assert first is not None and again is None
    assert session.scalar(sa.select(sa.func.count()).select_from(models.EventRow)) == 1
    assert first.trace_id == first.id  # 묶음의 첫 봉투


def test_big_payload_is_refused(session):
    with pytest.raises(ValueError, match="64KB"):
        emit(
            session,
            "field.changed",
            {
                "field": "f",
                "value_type": "text",
                "to_version_id": "v",
                "sources": ["L"] * 20000,
                "confidence": 1.0,
            },
            producer="svc:engine",
            key="big",
        )


def test_subscription_puts_one_job_per_receiver(session, monkeypatch):
    monkeypatch.setattr(events, "SUBSCRIPTIONS", {})
    events.subscribe(Subscription("audit", "alert.*", "audit_job"))
    events.subscribe(Subscription("other-app", "alert.*", "x", app="app:other@1"))
    body = {"alert_id": "a1", "field": "f", "to_version_id": "v"}
    ref = Ref(app="app:mine@1", item="item:i1")
    env = emit(session, "alert.sent", body, producer="svc:notify", key="a1:sent", ref=ref)
    jobs = session.scalars(sa.select(models.Job)).all()
    assert [(j.kind, j.dedupe_key) for j in jobs] == [("audit_job", f"{env.id}:audit")]
    assert jobs[0].payload == {"event_id": env.id, "trace": env.trace_id}
    # 받은 쪽은 작업 본문으로 추적과 원인을 이어 쓴다
    t = Trace.of(jobs[0].payload)
    assert (t.trace, t.cause) == (env.trace_id, env.id)


def test_pipeline_leaves_a_chain_from_collection_to_sent_alert(tmp_path):
    env = Env(tmp_path)
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()

    with env.factory() as s:
        rows = s.scalars(sa.select(models.EventRow).order_by(models.EventRow.id)).all()
        by_id = {r.id: r for r in rows}
        types = [r.type for r in rows]
        sent = next(r for r in rows if r.type == "alert.sent")

        # 원인을 거꾸로 따라가면 두 번째 수집 실행까지 닿는다(지금 화면의 "알림 근거"를 플랫폼 전체로)
        chain, cur = [], sent
        while cur is not None:
            chain.append(cur.type)
            cur = by_id.get(cur.causation_id)
        assert chain == [
            "alert.sent",
            "alert.created",
            "verdict.changed",
            "change.detected",
            "document.parsed",
            "document.version_created",
            "source.run_finished",
        ]
        runs = [r for r in rows if r.type == "source.run_finished"]
        assert sent.trace_id == runs[-1].id  # 두 번째 수집 실행이 이 일의 묶음이다

        for t in ("app.version_enabled", "field.changed", "alert.created"):
            assert t in types
        assert types.count("source.run_finished") == 2
        assert sent.app == "app:grant-alerts@1" and sent.subject == "person:u1"
        assert sent.item.startswith("item:") and sent.item_version == 2

        # 개인정보 값(프로필 속성)은 어느 봉투에도 없다. 사람은 가명 ID로만 가리킨다
        dump = json.dumps([r.payload for r in rows], ensure_ascii=False)
        assert (
            '"business_age_months": 72' not in dump
        )  # 이 사람의 값(규칙 원문은 공개 문서라 남는다)
        verdict = next(r for r in rows if r.type == "verdict.changed")
        assert set(verdict.payload) == {"field", "to_version_id", "before", "after", "evidence"}

        # 알림 발송은 봉투를 받아 돌았다(직접 등록한 작업이 아니다)
        notify = s.scalars(sa.select(models.Job).where(models.Job.kind == "notify")).one()
        assert notify.payload["event_id"] == next(r.id for r in rows if r.type == "alert.created")


def test_drift_and_consent_leave_envelopes(session):
    from noticedelta import drift
    from noticedelta.web import consent

    w = drift.Watch(session, "src")
    before = {str(i): {"k": "a"} for i in range(20)}
    now = {str(i): {"k": "b"} for i in range(20)}
    w.observe("r1", before, now)
    drift.decide(session, "src", "k", drift.IGNORED)

    session.add(models.Person(id="p1"))
    profile = models.Profile(person_id="p1", attributes={})
    session.add(profile)
    consent.record(session, profile, research_ok=False, at=T0)
    consent.set_research(session, profile, True, at=T0 + timedelta(days=1))
    consent.set_research(session, profile, True, at=T0 + timedelta(days=2))  # 그대로면 내지 않는다

    rows = session.scalars(sa.select(models.EventRow).order_by(models.EventRow.id)).all()
    assert sorted(r.type for r in rows) == [
        "consent.granted",
        "consent.granted",
        "source.drift_decided",
        "source.drift_held",
    ]
    granted = [r for r in rows if r.type == "consent.granted"]
    assert [g.payload["purpose"] for g in granted] == [["judge", "notify"], ["research"]]
    assert {g.subject for g in granted} == {"person:p1"}
