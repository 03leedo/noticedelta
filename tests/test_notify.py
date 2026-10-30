"""알림 발송의 상태 기계와 붙잡기 정책 (작업 목록 17번)."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.db import models, repo
from noticedelta.impact import Alert
from noticedelta.notify import deliver, inbox, prepare, release, render

T0 = datetime(2026, 11, 1, tzinfo=UTC)


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, alert, message):
        self.sent.append((alert.user_id, message))


@pytest.fixture
def s():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as session:
        for row in [
            models.Source(id="bizinfo", name="기업마당"),
            models.Program(id="PG", title="(가상) 창업 지원", title_key="k"),
            models.Notice(id="N", source_id="bizinfo", external_id="X", title="t"),
        ]:
            session.add(row)
            session.flush()
        for v in (1, 2, 3):
            session.add(
                models.NoticeVersion(
                    id=f"NV{v}", notice_id="N", version_no=v, kind="original", content_hash=f"h{v}"
                )
            )
            session.flush()
            session.add(
                models.ProgramVersion(
                    id=f"PV{v}",
                    program_id="PG",
                    version_no=v,
                    notice_version_id=f"NV{v}",
                    content_hash=f"h{v}",
                )
            )
            session.flush()
        yield session


def alert(s, user, version, before, after, field="eligibility_rules", confidence=1.0):
    kind = "judgment_changed" if field == "eligibility_rules" else "field_changed"
    key = f"{user}:PG:{field}:PV{version}"
    a = Alert(user, "PG", field, f"PV{version}", kind, before, after, [], key, confidence)
    assert repo.enqueue_alert(s, a)
    s.flush()
    return s.scalar(sa.select(models.OutboxAlert).where(models.OutboxAlert.idempotency_key == key))


def statuses(s):
    rows = s.scalars(sa.select(models.OutboxAlert).order_by(models.OutboxAlert.created_at)).all()
    return {r.idempotency_key: r.status for r in rows}


def clock():
    c = itertools.count()
    return lambda: T0 + timedelta(minutes=next(c))


def test_ready_alerts_are_sent_once_with_a_message(s):
    alert(s, "u1", 2, "eligible", "ineligible")
    assert prepare(s, "PG").ready == 1
    ch = Recorder()
    assert deliver(s, ch, now=clock()) == 1
    assert deliver(s, ch, now=clock()) == 0  # 같은 알림은 두 번 나가지 않는다
    assert ch.sent == [
        ("u1", "(가상) 창업 지원: 신청 자격 판정이 해당에서 비해당(으)로 바뀌었습니다.")
    ]
    assert [a.payload["message"] for a in inbox(s, "u1")] == [ch.sent[0][1]]


def test_newer_unsent_alert_supersedes_the_older(s):
    alert(s, "u1", 2, "2026-11-07", "2026-11-14", field="deadline")
    alert(s, "u1", 3, "2026-11-14", "2026-11-21", field="deadline")
    p = prepare(s, "PG")
    assert (p.ready, p.superseded) == (1, 1)
    ch = Recorder()
    deliver(s, ch, now=clock())
    # 두 번의 변경이 하나로 합쳐진다: 처음 값 → 마지막 값
    assert ch.sent == [
        ("u1", "(가상) 창업 지원: 마감일이(가) 2026-11-07에서 2026-11-21(으)로 바뀌었습니다.")
    ]


def test_unsent_changes_that_cancel_out_are_discarded(s):
    alert(s, "u1", 2, "eligible", "ineligible")
    alert(s, "u1", 3, "ineligible", "eligible")
    p = prepare(s, "PG")
    assert (p.ready, p.superseded, p.discarded) == (0, 1, 1)
    assert deliver(s, Recorder(), now=clock()) == 0


def test_reversing_a_sent_alert_marks_it_corrected(s):
    first = alert(s, "u1", 2, "eligible", "ineligible")
    prepare(s, "PG")
    deliver(s, Recorder(), now=clock())
    alert(s, "u1", 3, "ineligible", "eligible")
    prepare(s, "PG")
    ch = Recorder()
    deliver(s, ch, now=clock())
    assert ch.sent[0][1].startswith("[정정] 앞서 보낸 알림을 바로잡습니다.")
    assert s.get(models.OutboxAlert, first.id).status == "corrected"


def test_mass_ineligibility_is_held_until_released(s):
    for i in range(3):
        alert(s, f"u{i}", 2, "eligible", "ineligible")
    alert(s, "u9", 2, "ineligible", "eligible")  # 해당이 된 사람은 붙잡지 않는다
    p = prepare(s, "PG", mass_impact=3)
    assert (p.held, p.ready) == (3, 1)
    held = s.scalars(sa.select(models.OutboxAlert).where(models.OutboxAlert.status == "held")).all()
    assert "3명이 비해당" in held[0].held_reason
    assert deliver(s, Recorder(), now=clock()) == 1
    assert release(s, [a.id for a in held]) == 3
    assert deliver(s, Recorder(), now=clock()) == 3


def test_low_confidence_hold_from_pipeline_stays_held(s):
    a = alert(s, "u1", 2, "eligible", "ineligible", confidence=0.4)
    a.status, a.held_reason = "held", "근거 확인 필요 (신뢰도 0.40)"
    s.flush()
    prepare(s, "PG")
    assert statuses(s) == {"u1:PG:eligibility_rules:PV2": "held"}
    assert deliver(s, Recorder(), now=clock()) == 0


def test_render_values():
    row = models.OutboxAlert(
        payload={
            "field": "amount",
            "kind": "field_changed",
            "before": 50_000_000,
            "after": 30_000_000,
        }
    )
    assert (
        render(row, "사업") == "사업: 지원 금액이(가) 50,000,000에서 30,000,000(으)로 바뀌었습니다."
    )
