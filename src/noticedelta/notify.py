"""알림 발송: 아웃박스의 상태 기계와 붙잡기 정책 (설계 7.8, 작업 목록 17번).

    pending ─┬─ held ── (운영자 해제) ─ ready
             ├─ ready ── sending ── sent ── (뒤집는 정정이 나가면) corrected
             ├─ superseded   같은 사용자·사업·필드의 더 새 알림이 아직 안 나갔을 때 옛것
             └─ discarded    안 나간 알림끼리 되돌려져 결과가 처음과 같을 때 (보낼 것이 없음)

prepare가 정책을 적용하고 deliver가 ready만 채널로 보낸다. 같은 알림은 상태로 한 번만 나간다.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import OutboxAlert, Program, ProgramVersion
from noticedelta.names import FIELD_NAMES, RESULT_NAMES

UNSENT = ("pending", "held", "ready")
MASS_IMPACT = 20  # 한 버전의 같은 필드 변경이 이만큼 넘는 사람을 비해당으로 바꾸면 붙잡는다


def _now() -> datetime:
    return datetime.now(UTC)


def _version_no(session: Session, pv_id: str) -> int:
    return (
        session.scalar(sa.select(ProgramVersion.version_no).where(ProgramVersion.id == pv_id)) or 0
    )


def _show(value) -> str:
    if isinstance(value, str):
        return RESULT_NAMES.get(value, value)
    if isinstance(value, list):
        return ", ".join(map(str, value))
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return "없음" if value is None else str(value)


def render(alert: OutboxAlert, title: str) -> str:
    """알림 본문. 원문을 길게 옮기지 않고 무엇이 어떻게 바뀌었는지만 쓴다(근거는 화면에서)."""
    p = alert.payload
    field = FIELD_NAMES.get(p["field"], p["field"])
    prefix = "[정정] 앞서 보낸 알림을 바로잡습니다. " if p.get("corrects") else ""
    if p["kind"] == "judgment_changed":
        body = f"{field} 판정이 {_show(p['before'])}에서 {_show(p['after'])}(으)로 바뀌었습니다."
    else:
        body = f"{field}이(가) {_show(p['before'])}에서 {_show(p['after'])}(으)로 바뀌었습니다."
    return f"{prefix}{title}: {body}"


@dataclass
class Prepared:
    ready: int = 0
    held: int = 0
    superseded: int = 0
    discarded: int = 0


def _key(a: OutboxAlert) -> tuple[str, str, str]:
    return (a.user_id, a.program_id, a.payload["field"])


def prepare(session: Session, program_id: str, *, mass_impact: int = MASS_IMPACT) -> Prepared:
    """사업 하나의 pending 알림에 정책을 적용한다."""
    out = Prepared()
    unsent = session.scalars(
        sa.select(OutboxAlert).where(
            OutboxAlert.program_id == program_id, OutboxAlert.status.in_(UNSENT)
        )
    ).all()
    if not any(a.status == "pending" for a in unsent):
        return out
    by_key: dict[tuple, list[OutboxAlert]] = defaultdict(list)
    for a in unsent:
        by_key[_key(a)].append(a)

    survivors = []
    for chain in by_key.values():
        chain.sort(key=lambda a: _version_no(session, a.payload["to_version_id"]))
        newest = chain[-1]
        for old in chain[:-1]:  # 더 새 알림이 있으니 옛것은 보내지 않는다
            old.status, out.superseded = "superseded", out.superseded + 1
        if len(chain) > 1:
            # 안 나간 변경을 하나로 합친다: 처음 상태 → 마지막 상태
            newest.payload = {**newest.payload, "before": chain[0].payload["before"]}
        if newest.payload["before"] == newest.payload["after"]:
            newest.status, out.discarded = "discarded", out.discarded + 1
            continue
        survivors.append(newest)

    # 한 버전의 같은 필드가 많은 사람을 비해당으로 바꾸면, 추출 오류일 수 있어 붙잡는다.
    losses: dict[tuple, int] = defaultdict(int)
    for a in survivors:
        p = a.payload
        if p["kind"] == "judgment_changed" and p["after"] == "ineligible":
            losses[(p["to_version_id"], p["field"])] += 1

    for a in survivors:
        p = a.payload
        sent = session.scalar(
            sa.select(OutboxAlert)
            .where(
                OutboxAlert.user_id == a.user_id,
                OutboxAlert.program_id == a.program_id,
                OutboxAlert.status == "sent",
                OutboxAlert.payload["field"].as_string() == p["field"],
            )
            .order_by(OutboxAlert.sent_at.desc())
        )
        if (
            sent is not None
            and sent.payload["after"] == p["before"]
            and sent.payload["before"] == p["after"]
        ):
            a.payload = {**p, "corrects": sent.id}  # 보낸 알림을 뒤집는다
        if a.status == "held":
            out.held += 1  # 이미 붙잡힌 것(근거 확인 필요)은 그대로
        elif (
            p["kind"] == "judgment_changed"
            and p["after"] == "ineligible"
            and losses.get((p["to_version_id"], p["field"]), 0) >= mass_impact
        ):
            a.status = "held"
            a.held_reason = (
                f"한 번의 변경으로 {losses[(p['to_version_id'], p['field'])]}명이 비해당이 됨"
            )
            out.held += 1
        elif a.status == "pending":
            a.status, out.ready = "ready", out.ready + 1
    session.flush()
    return out


def release(session: Session, alert_ids: Sequence[str]) -> int:
    """운영자가 확인한 붙잡힌 알림을 보낼 수 있게 한다."""
    rows = session.scalars(
        sa.select(OutboxAlert).where(OutboxAlert.id.in_(alert_ids), OutboxAlert.status == "held")
    ).all()
    for a in rows:
        a.status, a.held_reason = "ready", None
    session.flush()
    return len(rows)


class Channel(Protocol):
    def send(self, alert: OutboxAlert, message: str) -> None: ...


class InboxChannel:
    """화면의 알림함. 보낼 곳이 따로 없고, sent 상태와 본문이 곧 알림함이다."""

    def send(self, alert: OutboxAlert, message: str) -> None:
        return None


def deliver(
    session: Session,
    channel: Channel,
    *,
    program_id: str | None = None,
    now: Callable[[], datetime] = _now,
) -> int:
    """ready 알림을 보낸다. 본문을 payload에 남기고 sent로 바꾼다. 보낸 수를 준다."""
    q = sa.select(OutboxAlert).where(OutboxAlert.status == "ready")
    if program_id is not None:
        q = q.where(OutboxAlert.program_id == program_id)
    sent = 0
    for a in session.scalars(q.order_by(OutboxAlert.created_at)).all():
        program = session.get(Program, a.program_id)
        message = render(a, program.title if program else a.program_id)
        a.status = "sending"
        session.flush()
        channel.send(a, message)  # 채널 쪽은 idempotency_key로 중복을 거른다
        a.payload = {**a.payload, "message": message}
        a.status, a.sent_at = "sent", now()
        corrects = a.payload.get("corrects")
        if corrects:
            old = session.get(OutboxAlert, corrects)
            if old is not None and old.status == "sent":
                old.status = "corrected"
        sent += 1
    session.flush()
    return sent


def inbox(session: Session, user_id: str, limit: int = 50) -> list[OutboxAlert]:
    return session.scalars(
        sa.select(OutboxAlert)
        .where(OutboxAlert.user_id == user_id, OutboxAlert.status.in_(("sent", "corrected")))
        .order_by(OutboxAlert.sent_at.desc())
        .limit(limit)
    ).all()
