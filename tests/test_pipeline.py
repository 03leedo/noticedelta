"""작업 종류로 이은 파이프라인: 적재 → 파싱 → 변경 기록 (작업 목록 13번)."""

import itertools
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_history import DOCS, RULES, ByContent, pdf

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import make_engine, models
from noticedelta.jobs import counts, run_worker
from noticedelta.parsing import ParserUnavailable
from noticedelta.pipeline import Context, enqueue_load, handlers
from noticedelta.snapshot import run_snapshot
from noticedelta.sources.bizinfo import to_record

HOST = "https://www.bizinfo.go.kr"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 30, tzinfo=UTC)

    def __call__(self):
        return self.t


class Down:
    def parse(self, data, mime):
        raise ParserUnavailable("docker 꺼짐")


class Env:
    def __init__(self, tmp_path):
        engine = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
        models.Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine)
        self.root = tmp_path / "snapshots"
        self.files: dict[str, bytes | None] = {}
        self.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (
                    httpx.Response(200, content=self.files[r.url.path])
                    if self.files.get(r.url.path)
                    else httpx.Response(503)
                )
            )
        )
        self.hours = itertools.count()
        self.clock = Clock()
        self.ctx = Context(
            root=self.root, store=LocalBlobStore(self.root / "blobs"), parser=ByContent()
        )

    def collect(self, deadline, attachment):
        self.files["/f/a.pdf"] = attachment
        raw = {
            "pblancId": "P1",
            "pblancNm": "2026년 (가상) 창업 지원 모집",
            "excInsttNm": "기관X",
            "reqstBeginEndDe": f"2026-10-13 ~ {deadline}",
            "bsnsSumryCn": f"<p>접수 마감 {deadline}</p>",
            "fileNm": "공고문.pdf",
            "flpthNm": f"{HOST}/f/a.pdf",
        }
        t = datetime(2026, 10, 23) + timedelta(hours=6 * next(self.hours))
        run_snapshot(
            {"bizinfo": lambda page: [to_record(raw)] if page == 1 else []},
            http=self.http,
            root=self.root,
            now=lambda: t,
        )
        with self.factory() as s:
            enqueue_load(s, self.root)
            s.commit()

    def work(self, parser=None):
        if parser is not None:
            self.ctx.parser = parser
        return run_worker(self.factory, handlers(self.ctx), worker_id="w", now=self.clock)

    def count(self, model):
        with self.factory() as s:
            return s.scalar(sa.select(sa.func.count()).select_from(model))

    def jobs(self):
        with self.factory() as s:
            return counts(s)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_runs_flow_from_load_to_change_events(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    stats = env.work()
    # 적재 2 + 파싱 2 + 비교 2
    assert (stats.done, stats.retried, stats.dead) == (6, 0, 0)
    assert env.count(models.LoadedRun) == 2
    assert env.count(models.ChangeEvent) == 2
    with env.factory() as s:
        ev = s.scalars(
            sa.select(models.ChangeEvent).where(models.ChangeEvent.from_version_id.is_not(None))
        ).one()
        assert ev.changed_lineages["modified"]


def test_nothing_new_means_no_new_jobs(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:
        assert enqueue_load(s, env.root) is None
    assert env.work().done == 0


def test_parser_outage_is_retried_later(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    stats = env.work(parser=Down())
    assert stats.retried == 1 and env.count(models.ChangeEvent) == 0
    env.clock.t += timedelta(hours=1)  # 재시도 시각이 지나고 파서가 돌아옴
    env.work(parser=ByContent())
    assert env.count(models.ChangeEvent) == 1
    assert env.jobs() == {"done": 3}  # 적재·파싱·비교, 파싱은 재시도 뒤 성공


def test_late_attachment_is_filled_and_compared(env):
    """첫 수집에서 첨부 받기가 실패했다. 다음 수집에서 받으면 같은 버전을 다시 파싱해 기록한다."""
    env.collect("2026-11-07", None)  # 503
    env.work()
    assert env.count(models.ChangeEvent) == 0  # 첨부가 없어 비교를 기다린다
    assert env.jobs().get("retry") == 1

    env.collect("2026-11-07", pdf(RULES, DOCS))  # 내용은 그대로, 첨부만 이번에 받음
    env.clock.t += timedelta(hours=1)
    env.work()
    assert env.count(models.ChangeEvent) == 1
    assert env.count(models.ProgramVersion) == 1
