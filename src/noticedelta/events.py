"""이벤트 봉투 (platform-services.md 2장). 층 사이에 일어난 일을 모두 같은 모양으로 남긴다.

    emit(session, "verdict.changed", {...}, producer="svc:engine", key="...", item=..., ...)

규칙 (2.3)
1. 추가만 한다. 고칠 일은 새 봉투로 남긴다.
2. 상태 변경과 봉투 쓰기는 한 트랜잭션이다. emit은 부른 쪽의 세션에 쓰고 커밋하지 않는다.
3. 같은 (type, idempotency_key)는 한 번만 남는다. 다시 내면 아무것도 하지 않고 None.
4. 개인정보 값을 넣지 않는다. 사람은 subject(가명 ID)로만 가리킨다. 타입마다 payload 칸을 정해 두고
   (extra=forbid) 그 밖의 칸은 받지 않는다.
5. 타입 등록부: 타입마다 판(v)과 payload 모양이 코드에 있다. 모르는 타입은 쓰지 않는다.

전달 (2.5): 메시지 버스를 두지 않는다. 구독(받는 쪽, 타입, 작업 종류)마다 같은 트랜잭션에서 작업 큐에
작업을 넣는다. 재시도·임대·실패 처리는 작업 큐가 이미 한다. 작업 본문에는 봉투 id(`event_id`)와
추적 id(`trace`)가 들어 있어, 받은 쪽이 다음 봉투의 원인과 추적을 이어 쓴다.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noticedelta.db.models import EventRow
from noticedelta.jobs import enqueue

MAX_PAYLOAD_BYTES = 64 * 1024
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


_last = [0, 0]  # (밀리초, 무작위 부분): 같은 밀리초 안에서도 순서를 지키려고 1씩 올린다


def new_ulid(at: datetime | None = None) -> str:
    """ULID: 앞 48비트가 밀리초 시각이라 문자열 순서가 시간 순서다(같은 밀리초 안에서도)."""
    ms = int((at.timestamp() if at else time.time()) * 1000)
    if ms <= _last[0]:  # 같은 밀리초이거나 시계가 뒤로 갔다: 앞 것보다 크게
        ms, rand = _last[0], _last[1] + 1
    else:
        rand = int.from_bytes(os.urandom(10), "big") >> 1  # 올릴 자리를 남긴다
    _last[0], _last[1] = ms, rand
    n = (ms << 80) | (rand & ((1 << 80) - 1))
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


# --- 타입 등록부 ---


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class EventType:
    name: str
    v: int
    payload: type[BaseModel]
    about: str


REGISTRY: dict[str, EventType] = {}


def event_type(name: str, v: int, about: str):
    def wrap(model: type[BaseModel]) -> type[BaseModel]:
        if name in REGISTRY:
            raise ValueError(f"이미 있는 봉투 타입: {name}")
        REGISTRY[name] = EventType(name, v, model, about)
        return model

    return wrap


@event_type("source.run_finished", 1, "수집 실행 하나를 DB에 넣었다")
class RunFinished(_Payload):
    run: str
    sources: dict[str, dict[str, int | str | None]]  # 출처마다 count, changed, error


@event_type("source.drift_held", 1, "형식 변화로 키 하나를 붙잡았다")
class DriftHeld(_Payload):
    source: str
    key: str
    kind: str  # changed / emptied / filled
    affected: int
    compared: int


@event_type("source.drift_decided", 1, "붙잡은 키를 사람이 정했다")
class DriftDecided(_Payload):
    source: str
    key: str
    status: str  # ignored / released
    decided_by: str


@event_type("document.version_created", 1, "항목의 새 판이 생겼다")
class VersionCreated(_Payload):
    item_version_id: str
    notice_version_id: str
    content_hash: str


@event_type("document.parsed", 1, "항목 판의 첨부를 파싱했다")
class Parsed(_Payload):
    item_version_id: str
    attachments: int
    unreadable: int


@event_type("change.detected", 1, "두 판을 조각으로 맞춰 바뀐 곳을 찾았다")
class ChangeDetected(_Payload):
    from_version_id: str | None
    to_version_id: str
    added: int
    removed: int
    modified: int
    matcher: str


@event_type("field.changed", 1, "필드 값이 진짜로 바뀌었다(타입별 비교)")
class FieldChanged(_Payload):
    field: str
    value_type: str
    to_version_id: str
    sources: list[str]  # 근거 조각 lineage
    confidence: float
    after: Any = None  # 짧은 값만(공개 문서에서 뽑은 값). 길면 None


@event_type("verdict.changed", 1, "사람 하나의 판정이 바뀌었다")
class VerdictChanged(_Payload):
    field: str
    to_version_id: str
    before: str
    after: str
    evidence: list[str]


class _Alert(_Payload):
    alert_id: str
    field: str
    to_version_id: str
    reason: str | None = None


for _name, _about in [
    ("alert.created", "알림을 아웃박스에 썼다"),
    ("alert.held", "알림을 붙잡았다(근거 부족, 대량 영향)"),
    ("alert.released", "붙잡은 알림을 사람이 풀었다"),
    ("alert.sent", "알림이 나갔다"),
    ("alert.corrected", "나간 알림을 바로잡았다"),
    ("alert.superseded", "더 새 알림이 있어 보내지 않는다"),
    ("alert.discarded", "결국 바뀐 것이 없어 보내지 않는다"),
]:
    REGISTRY[_name] = EventType(_name, 1, _Alert, _about)


@event_type("app.version_enabled", 1, "앱의 판 하나를 켰다")
class AppEnabled(_Payload):
    app_id: str
    version: int
    content_hash: str


class _Consent(_Payload):
    purpose: list[str]
    text_version: str | None = None


REGISTRY["consent.granted"] = EventType("consent.granted", 1, _Consent, "사람이 동의했다")
REGISTRY["consent.withdrawn"] = EventType("consent.withdrawn", 1, _Consent, "사람이 동의를 거뒀다")


# --- 구독 ---


@dataclass(frozen=True)
class Subscription:
    """봉투를 받아 작업을 넣는 쪽. 타입 패턴(fnmatch)과 앱으로 거른다."""

    name: str
    pattern: str
    job: str
    dedupe: Callable[[EventRow], str] | None = None  # 기본은 봉투 id + 받는 쪽
    concurrency: Callable[[EventRow], str | None] | None = None
    payload: Callable[[EventRow], Mapping[str, Any]] | None = None
    priority: int = 100
    app: str | None = None  # 이 앱의 봉투만


SUBSCRIPTIONS: dict[str, Subscription] = {}


def subscribe(sub: Subscription) -> None:
    """같은 이름은 덮어쓴다(모듈을 다시 읽어도 하나)."""
    SUBSCRIPTIONS[sub.name] = sub


def _route(session: Session, env: EventRow) -> int:
    n = 0
    for sub in SUBSCRIPTIONS.values():
        if not fnmatchcase(env.type, sub.pattern) or (sub.app and env.app != sub.app):
            continue
        body = {
            "event_id": env.id,
            "trace": env.trace_id or env.id,
            **(sub.payload(env) if sub.payload else {}),
        }
        job = enqueue(
            session,
            sub.job,
            body,
            dedupe_key=sub.dedupe(env) if sub.dedupe else f"{env.id}:{sub.name}",
            concurrency_key=sub.concurrency(env) if sub.concurrency else None,
            priority=sub.priority,
        )
        n += job is not None
    return n


# --- 내기 ---


@dataclass
class Trace:
    """작업 사이에 이어 주는 추적: 이 일의 묶음(trace)과 이 일을 일으킨 봉투(cause)."""

    trace: str | None = None
    cause: str | None = None

    @classmethod
    def of(cls, job_payload: Mapping[str, Any] | None) -> Trace:
        p = job_payload or {}
        return cls(p.get("trace"), p.get("event_id") or p.get("cause"))

    def carry(self, env: EventRow | None = None) -> dict[str, str]:
        """다음 작업 본문에 넣을 값."""
        out = {}
        trace = self.trace or (env.trace_id if env is not None else None)
        if trace:
            out["trace"] = trace
        cause = env.id if env is not None else self.cause
        if cause:
            out["cause"] = cause
        return out


@dataclass
class Ref:
    """봉투가 가리키는 것(해당될 때만)."""

    app: str | None = None  # "app:<id>@<판>"
    subject: str | None = None  # "person:<id>"
    item: str | None = None  # "item:<id>"
    item_version: int | None = None
    labels: dict[str, Any] = field(default_factory=dict)


def app_ref(app_id: str, version: int) -> str:
    return f"app:{app_id}@{version}"


def emit(
    session: Session,
    type_name: str,
    payload: Mapping[str, Any],
    *,
    producer: str,
    key: str,
    ref: Ref | None = None,
    trace: Trace | None = None,
    occurred_at: datetime | None = None,
    now: datetime | None = None,
) -> EventRow | None:
    """봉투 하나를 쓰고 구독마다 작업을 넣는다. 같은 (타입, 키)가 이미 있으면 None."""
    t = REGISTRY.get(type_name)
    if t is None:
        raise KeyError(f"등록되지 않은 봉투 타입 {type_name}. events.REGISTRY에 먼저 적는다")
    body = t.payload.model_validate(dict(payload)).model_dump(mode="json")
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{type_name} 본문이 64KB를 넘는다. 큰 것은 원문 저장소 키만 넣는다")
    ref, trace = ref or Ref(), trace or Trace()
    recorded = now or datetime.now(UTC)
    env = EventRow(
        id=new_ulid(recorded),
        type=type_name,
        v=t.v,
        occurred_at=occurred_at or recorded,
        recorded_at=recorded,
        producer=producer,
        app=ref.app,
        subject=ref.subject,
        item=ref.item,
        item_version=ref.item_version,
        trace_id=trace.trace,
        causation_id=trace.cause,
        idempotency_key=key,
        labels=dict(ref.labels),
        payload=body,
    )
    if env.trace_id is None:
        env.trace_id = env.id  # 묶음의 첫 봉투
    try:
        with session.begin_nested():
            session.add(env)
    except IntegrityError:
        return None
    _route(session, env)
    return env


Status = Literal["created", "held", "released", "sent", "corrected", "superseded", "discarded"]
