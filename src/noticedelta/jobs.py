"""PostgreSQL 작업 큐와 단일 워커 (설계 8장, 2026-10-23 결정).

- 꺼내기는 `FOR UPDATE SKIP LOCKED`로 여러 워커가 같은 작업을 잡지 않게 한다(SQLite에서는 무시되고
  단일 쓰기로 같은 효과).
- 같은 `concurrency_key`(사업 하나)의 작업은 동시에 하나만 돈다. 사업 단위 처리는 직전 버전이
  기준이라 직렬이어야 한다.
- 임대(`lease_until`)가 지난 running 작업은 워커가 죽은 것으로 보고 다시 가져간다.
- 꺼내기는 짧은 트랜잭션으로 바로 커밋한다(다른 워커가 running을 보게). 핸들러는 다음 트랜잭션에서
  돌고, 결과 기록과 다음 작업 등록(`enqueue`)이 작업 완료와 함께 커밋되거나 함께 취소된다.
- 실패는 종류로 나눈다. 일시 장애는 지수 백오프로 다시 하고, 영구 실패나 횟수 초과는 dead로 보낸다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from noticedelta.db.models import Job

LEASE = timedelta(minutes=10)
BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_MAX = timedelta(hours=6)
CLAIM_CANDIDATES = 10


class TransientError(Exception):
    """다시 하면 될 수 있는 실패 (네트워크, 429·5xx, 파서 컨테이너를 못 띄움)."""


class PermanentError(Exception):
    """다시 해도 같은 실패 (설정 오류, 형식이 맞지 않는 입력). 바로 dead로 보낸다."""


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    session: Session,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    dedupe_key: str,
    concurrency_key: str | None = None,
    priority: int = 100,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job | None:
    """작업을 등록한다. 같은 dedupe_key가 이미 있으면 None (멱등)."""
    job = Job(
        kind=kind,
        payload=dict(payload or {}),
        dedupe_key=dedupe_key,
        concurrency_key=concurrency_key,
        priority=priority,
        run_after=run_after or _now(),
        max_attempts=max_attempts,
    )
    try:
        with session.begin_nested():
            session.add(job)
    except IntegrityError:
        return None
    return job


def claim(
    session: Session,
    worker_id: str,
    *,
    kinds: set[str] | None = None,
    now: datetime | None = None,
    lease: timedelta = LEASE,
) -> Job | None:
    """실행할 작업 하나를 꺼내 running으로 바꾼다. 없으면 None."""
    now = now or _now()
    running = aliased(Job)
    busy = (
        sa.select(running.id)
        .where(
            running.concurrency_key == Job.concurrency_key,
            running.status == "running",
            running.lease_until > now,
            running.id != Job.id,
        )
        .exists()
    )
    ready = sa.or_(
        sa.and_(Job.status.in_(("queued", "retry")), Job.run_after <= now),
        sa.and_(Job.status == "running", Job.lease_until <= now),  # 죽은 워커의 작업
    )
    base = (
        sa.select(Job)
        .where(ready)
        .where(sa.or_(Job.concurrency_key.is_(None), ~busy))
        .order_by(Job.priority, Job.id)
        .limit(1)  # 한 번에 한 행만 잠근다. 여러 행을 잠그면 다른 워커가 잡을 것이 없어진다
        .with_for_update(skip_locked=True)
    )
    if kinds:
        base = base.where(Job.kind.in_(kinds))
    postgres = session.get_bind().dialect.name == "postgresql"
    skipped: list[int] = []
    for _ in range(CLAIM_CANDIDATES):
        stmt = base.where(Job.id.not_in(skipped)) if skipped else base
        job = session.scalar(stmt)
        if job is None:
            return None
        if job.concurrency_key and postgres:
            # 두 워커가 같은 사업의 다른 작업을 동시에 고르는 경쟁을 막는다. 잠금은 이 트랜잭션
            # (claim 커밋)까지만 잡고, 그 뒤에는 커밋된 running 행이 busy 조건으로 막는다.
            got = session.scalar(
                sa.select(sa.func.pg_try_advisory_xact_lock(sa.func.hashtext(job.concurrency_key)))
            )
            if not got or session.scalar(sa.select(busy).where(Job.id == job.id)):
                skipped.append(job.id)
                continue
        job.status = "running"
        job.attempts += 1
        job.locked_by = worker_id
        job.lease_until = now + lease
        job.started_at = now
        session.flush()
        return job
    return None


def complete(session: Session, job: Job, *, now: datetime | None = None) -> None:
    job.status = "done"
    job.finished_at = now or _now()
    job.lease_until = None
    job.error_class = None
    session.flush()


def fail(session: Session, job: Job, error: BaseException, *, now: datetime | None = None) -> None:
    now = now or _now()
    permanent = isinstance(error, PermanentError)
    job.error_class = "permanent" if permanent else "transient"
    job.last_error = f"{type(error).__name__}: {error}"[:2000]
    job.lease_until = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = "dead"
    else:
        job.status = "retry"
        job.run_after = now + min(BACKOFF_BASE * 2 ** (job.attempts - 1), BACKOFF_MAX)
    session.flush()


Handler = Callable[[Session, Job], None]


@dataclass
class WorkerStats:
    done: int = 0
    retried: int = 0
    dead: int = 0


def run_worker(
    session_factory: Callable[[], Session],
    handlers: Mapping[str, Handler],
    *,
    worker_id: str,
    max_jobs: int | None = None,
    now: Callable[[], datetime] = _now,
) -> WorkerStats:
    """꺼낼 작업이 없을 때까지(또는 max_jobs까지) 처리한다. 크론이나 예약 작업이 주기적으로 부른다.

    트랜잭션 셋으로 나눈다. ① 꺼내기(running + 임대)를 바로 커밋해 다른 워커가 보게 한다.
    ② 핸들러와 완료 표시를 한 트랜잭션으로 묶는다(결과 기록과 다음 작업 등록이 함께 커밋되거나
    함께 취소). ③ 핸들러가 실패하면 그 트랜잭션은 되돌리고 실패만 새로 기록한다.
    ②에서 워커가 죽으면 임대가 끝난 뒤 다른 실행이 다시 가져간다.
    """
    stats = WorkerStats()
    while max_jobs is None or stats.done + stats.retried + stats.dead < max_jobs:
        with session_factory() as session:
            job = claim(session, worker_id, kinds=set(handlers), now=now())
            session.commit()
            if job is None:
                return stats
            job_id = job.id
        error: Exception | None = None
        with session_factory() as session:
            job = session.get(Job, job_id)
            try:
                handlers[job.kind](session, job)
                complete(session, job, now=now())
                session.commit()
                stats.done += 1
            except Exception as e:  # 핸들러의 모든 실패를 작업 상태로 남긴다
                error = e
                session.rollback()
        if error is None:
            continue
        with session_factory() as session:
            job = session.get(Job, job_id)
            fail(session, job, error, now=now())
            session.commit()
            stats.dead += job.status == "dead"
            stats.retried += job.status == "retry"
    return stats


def counts(session: Session) -> dict[str, int]:
    rows = session.execute(sa.select(Job.status, sa.func.count()).group_by(Job.status)).all()
    return {status: n for status, n in rows}
