import os
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta.db import make_engine, models
from noticedelta.db.models import Job
from noticedelta.jobs import (
    BACKOFF_BASE,
    PermanentError,
    TransientError,
    claim,
    counts,
    enqueue,
    run_worker,
)

T0 = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_factory(url):
    engine = make_engine(url)
    models.Base.metadata.drop_all(engine, tables=[Job.__table__])
    models.Base.metadata.create_all(engine, tables=[Job.__table__])
    return sessionmaker(engine)


@pytest.fixture
def factory(tmp_path):
    return make_factory(f"sqlite:///{tmp_path / 'jobs.db'}")


def test_enqueue_is_idempotent(factory):
    with factory() as s:
        assert enqueue(s, "parse", {"a": 1}, dedupe_key="parse:x", run_after=T0) is not None
        assert enqueue(s, "parse", {"a": 2}, dedupe_key="parse:x", run_after=T0) is None
        s.commit()
        assert s.scalar(sa.select(sa.func.count()).select_from(Job)) == 1


def test_claim_respects_priority_run_after_and_kinds(factory):
    with factory() as s:
        enqueue(s, "parse", dedupe_key="low", priority=200, run_after=T0)
        enqueue(s, "parse", dedupe_key="high", priority=10, run_after=T0)
        enqueue(s, "parse", dedupe_key="later", priority=0, run_after=T0 + timedelta(hours=1))
        enqueue(s, "extract", dedupe_key="other", priority=0, run_after=T0)
        s.commit()
        job = claim(s, "w1", kinds={"parse"}, now=T0)
        assert job.dedupe_key == "high" and job.status == "running" and job.attempts == 1


def test_same_concurrency_key_runs_one_at_a_time(factory):
    with factory() as s:
        enqueue(s, "impact", dedupe_key="a1", concurrency_key="program:A", run_after=T0)
        enqueue(s, "impact", dedupe_key="a2", concurrency_key="program:A", run_after=T0)
        enqueue(s, "impact", dedupe_key="b1", concurrency_key="program:B", run_after=T0)
        s.commit()
        first = claim(s, "w1", now=T0).dedupe_key
        s.commit()
    with factory() as s:
        second = claim(s, "w2", now=T0).dedupe_key
        s.commit()
        assert (first, second) == ("a1", "b1")
        assert claim(s, "w3", now=T0) is None  # a2는 a1이 끝날 때까지 기다린다


def test_expired_lease_is_reclaimed(factory):
    with factory() as s:
        enqueue(s, "parse", dedupe_key="x", run_after=T0)
        s.commit()
        claim(s, "dead-worker", now=T0)
        s.commit()
        assert claim(s, "w2", now=T0 + timedelta(minutes=5)) is None
        job = claim(s, "w2", now=T0 + timedelta(minutes=11))
        assert job.locked_by == "w2" and job.attempts == 2


def test_worker_completes_and_enqueues_next_atomically(factory):
    with factory() as s:
        enqueue(s, "fetch", {"id": "N1"}, dedupe_key="fetch:N1", run_after=T0)
        s.commit()

    def fetch(session, job):
        enqueue(
            session, "parse", job.payload, dedupe_key=f"parse:{job.payload['id']}", run_after=T0
        )

    def parse(session, job):
        pass

    stats = run_worker(factory, {"fetch": fetch, "parse": parse}, worker_id="w", now=Clock())
    assert stats.done == 2
    with factory() as s:
        assert counts(s) == {"done": 2}


def test_transient_failure_backs_off_then_dies(factory):
    with factory() as s:
        enqueue(s, "parse", dedupe_key="x", run_after=T0, max_attempts=3)
        s.commit()
    clock = Clock()

    def flaky(session, job):
        enqueue(session, "next", dedupe_key="should-not-exist", run_after=T0)
        raise TransientError("parser container did not start")

    stats = run_worker(factory, {"parse": flaky}, worker_id="w", now=clock)
    assert stats.retried == 1
    with factory() as s:
        job = s.scalar(sa.select(Job).where(Job.dedupe_key == "x"))
        assert (job.status, job.attempts, job.error_class) == ("retry", 1, "transient")
        assert job.run_after.replace(tzinfo=UTC) == T0 + BACKOFF_BASE
        # 실패한 핸들러가 등록한 다음 작업은 함께 되돌아갔다
        assert s.scalar(sa.select(Job).where(Job.dedupe_key == "should-not-exist")) is None

    for _ in range(2):
        clock.t += timedelta(hours=1)
        run_worker(factory, {"parse": flaky}, worker_id="w", now=clock)
    with factory() as s:
        job = s.scalar(sa.select(Job).where(Job.dedupe_key == "x"))
        assert (job.status, job.attempts) == ("dead", 3)


def test_permanent_failure_dies_immediately(factory):
    with factory() as s:
        enqueue(s, "parse", dedupe_key="x", run_after=T0)
        s.commit()

    def bad(session, job):
        raise PermanentError("인증키 없음")

    assert run_worker(factory, {"parse": bad}, worker_id="w", now=Clock()).dead == 1
    with factory() as s:
        assert s.scalar(sa.select(Job.error_class)) == "permanent"


# --- PostgreSQL 통합 (NOTICEDELTA_PG_URL이 있을 때만) ---

PG_URL = os.environ.get("NOTICEDELTA_PG_URL")
pg = pytest.mark.skipif(not PG_URL, reason="NOTICEDELTA_PG_URL 없음")


@pg
def test_postgres_skip_locked_gives_different_jobs_to_concurrent_workers():
    factory = make_factory(PG_URL)
    with factory() as s:
        for i in range(3):
            enqueue(s, "parse", dedupe_key=f"j{i}", run_after=T0)
        s.commit()
    s1, s2 = factory(), factory()
    try:
        a = claim(s1, "w1", now=T0)  # s1이 커밋하지 않고 행 잠금을 쥔 채로
        b = claim(s2, "w2", now=T0)
        assert a.dedupe_key != b.dedupe_key
    finally:
        s1.rollback(), s2.rollback(), s1.close(), s2.close()


@pg
def test_postgres_same_key_is_not_claimed_twice_concurrently():
    factory = make_factory(PG_URL)
    with factory() as s:
        enqueue(s, "impact", dedupe_key="a1", concurrency_key="program:A", run_after=T0)
        enqueue(s, "impact", dedupe_key="a2", concurrency_key="program:A", run_after=T0)
        s.commit()
    s1, s2 = factory(), factory()
    try:
        a = claim(s1, "w1", now=T0)  # 커밋 전: advisory lock을 쥐고 있다
        b = claim(s2, "w2", now=T0)
        assert a.dedupe_key == "a1" and b is None
        s1.commit()
        s2.rollback()
        assert claim(s2, "w2", now=T0) is None  # 커밋 뒤에는 running 행이 막는다
    finally:
        s1.rollback(), s2.rollback(), s1.close(), s2.close()
