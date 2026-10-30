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
        # 작업 등록 시각(실제 시각)보다 늦게 시작한다. 고정 날짜는 그날이 지나면 작업을 못 꺼낸다.
        self.t = datetime.now(UTC) + timedelta(minutes=1)

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
            root=self.root,
            store=LocalBlobStore(self.root / "blobs"),
            parser=ByContent(),
            now=self.clock,
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
    assert env.jobs().get("queued") == 1  # 비교는 30분 뒤로 미뤄 둠

    env.collect("2026-11-07", pdf(RULES, DOCS))  # 내용은 그대로, 첨부만 이번에 받음
    env.clock.t += timedelta(hours=1)
    env.work()
    assert env.count(models.ChangeEvent) == 1
    assert env.count(models.ProgramVersion) == 1


class RulesFromText:
    """“창업 N년 이내”를 자격 규칙으로 뽑는 가짜 추출기 (LLM 대신)."""

    def __init__(self, confidence=1.0):
        self.confidence = confidence
        self.calls = []

    def __call__(self, fields, segments):
        import re

        from noticedelta.impact import FieldValue

        self.calls.append(sorted(fields))
        for s in segments:
            m = re.search(r"창업 (\d+)년", s.text)
            if m and "eligibility_rules" in fields:
                rules = {
                    "group": "all_of",
                    "rules": [
                        {
                            "attribute": "business_age_months",
                            "op": "<=",
                            "value": int(m.group(1)) * 12,
                            "confidence": self.confidence,
                        }
                    ],
                }
                return {
                    "eligibility_rules": FieldValue(
                        "eligibility_rules", "rules", rules, [s.lineage_id], self.confidence
                    )
                }
        return {}


def subscribe_eligible_user(env):
    """v1을 본 사용자: 업력 6년, 자격 규칙을 구독, 판정은 해당."""
    with env.factory() as s:
        pv = s.scalars(sa.select(models.ProgramVersion)).one()
        s.add(models.Profile(user_id="u1", attributes={"business_age_months": 72}))
        s.flush()
        s.add(
            models.SubscriptionRow(
                user_id="u1", program_id=pv.program_id, fields=["eligibility_rules"]
            )
        )
        s.add(
            models.JudgmentRow(
                user_id="u1",
                program_id=pv.program_id,
                program_version_id=pv.id,
                ruleset_hash="v1",
                result="eligible",
            )
        )
        s.commit()


def test_correction_reaches_outbox_with_rejudgment(env):
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    assert env.count(models.FieldValueRow) == 1  # v1: 자격 규칙(창업 7년)
    subscribe_eligible_user(env)

    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        alert = s.scalars(sa.select(models.OutboxAlert)).one()
        assert alert.user_id == "u1" and alert.status == "pending"
        assert (alert.payload["before"], alert.payload["after"]) == ("eligible", "ineligible")
        latest = s.scalars(
            sa.select(models.JudgmentRow).order_by(models.JudgmentRow.created_at.desc())
        ).first()
        assert latest.result == "ineligible"
    # 두 번째 추출은 바뀐 조각(본문의 마감일, 첨부의 자격 문단)에 걸린 필드만 물었다
    first, second = env.ctx.extractor.calls
    assert second == ["deadline", "eligibility_rules"] and len(second) < len(first)


def test_low_confidence_change_is_held(env):
    env.ctx.extractor = RulesFromText(confidence=0.5)
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        alert = s.scalars(sa.select(models.OutboxAlert)).one()
        assert alert.status == "held" and "근거 확인" in alert.held_reason


def test_without_extractor_pipeline_stops_at_change_events(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:
        kinds = set(s.scalars(sa.select(models.Job.kind)).all())
    assert kinds == {"load_run", "parse", "diff"}


def test_backfill_registers_versions_loaded_before_the_queue(env):
    from noticedelta.pipeline import enqueue_backfill
    from noticedelta.programs import load_snapshots

    env.collect("2026-11-07", pdf(RULES, DOCS))
    with env.factory() as s:  # 작업 큐 없이 예전 방식으로 적재
        load_snapshots(s, env.root, env.ctx.store)
        s.execute(sa.delete(models.Job))
        assert enqueue_backfill(s) == 1
        s.commit()
    env.work()
    assert env.count(models.ChangeEvent) == 1
