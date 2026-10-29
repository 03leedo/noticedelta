"""작업 종류로 이은 파이프라인 (설계 5장, 8장, 작업 목록 13번).

    적재(load_run) → 파싱(parse) → 변경 기록(diff) → [추출·영향 계산은 다음 단계]

각 핸들러는 자기 단계의 결과와 다음 작업 등록을 한 트랜잭션에 쓴다(run_worker가 커밋). 같은 일은
dedupe_key로 한 번만 등록되고, 사업 하나의 작업은 concurrency_key로 하나씩만 돈다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.blobstore import BlobStore
from noticedelta.db.models import Attachment, Job, LoadedRun, ProgramVersion
from noticedelta.history import MATCHER_VERSION, record_program
from noticedelta.ingest import parse_attachments
from noticedelta.jobs import Handler, PermanentError, TransientError, enqueue
from noticedelta.parsing import Parser, ParserUnavailable
from noticedelta.programs import LoadSummary, load_run

LOAD, PARSE, DIFF = "load_run", "parse", "diff"
LOADER_KEY = "loader"  # 실행 파일은 시간 순으로 하나씩 넣는다


@dataclass
class Context:
    root: Path  # 스냅샷 폴더 (runs/, blobs/)
    store: BlobStore
    parser: Parser


def _unloaded_runs(session: Session, root: Path) -> list[str]:
    loaded = set(session.scalars(sa.select(LoadedRun.name)).all())
    return [p.stem for p in sorted((root / "runs").glob("*.json")) if p.stem not in loaded]


def enqueue_load(session: Session, root: Path) -> Job | None:
    """아직 넣지 않은 실행 중 가장 이른 것의 적재 작업을 등록한다. 수집이 끝날 때 부른다."""
    pending = _unloaded_runs(session, root)
    if not pending:
        return None
    return enqueue(
        session,
        LOAD,
        {"run": pending[0]},
        dedupe_key=f"{LOAD}:{pending[0]}",
        concurrency_key=LOADER_KEY,
        priority=10,
    )


def _program_of(session: Session, pv_id: str) -> str:
    return session.scalar(sa.select(ProgramVersion.program_id).where(ProgramVersion.id == pv_id))


def _enqueue_parse(session: Session, pv_id: str) -> None:
    pv = session.get(ProgramVersion, pv_id)
    blobs = session.scalar(
        sa.select(sa.func.count()).where(
            Attachment.notice_version_id == pv.notice_version_id, Attachment.blob_key.is_not(None)
        )
    )
    # 첨부가 늦게 채워지면 받은 첨부 수가 달라져 새 작업이 된다.
    enqueue(
        session,
        PARSE,
        {"program_version_id": pv_id},
        dedupe_key=f"{PARSE}:{pv_id}:{blobs}",
        concurrency_key=f"program:{pv.program_id}",
        priority=50,
    )


def handle_load(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        pending = _unloaded_runs(session, ctx.root)
        if not pending:
            return
        # 등록된 실행이 아니라 가장 이른 실행을 넣는다. 순서가 어긋나지 않게.
        path = ctx.root / "runs" / f"{pending[0]}.json"
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PermanentError(f"실행 파일을 읽지 못함: {path.name}") from e
        summary = LoadSummary()
        load_run(session, path, ctx.store, summary)
        for pv_id in summary.new_version_ids + summary.refilled_version_ids:
            _enqueue_parse(session, pv_id)
        enqueue_load(session, ctx.root)  # 남은 실행이 있으면 다음 것

    return handle


def handle_parse(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        pv = session.get(ProgramVersion, job.payload["program_version_id"])
        if pv is None:
            raise PermanentError("사업 버전이 없음")
        try:
            parse_attachments(session, pv.notice_version_id, parser=ctx.parser, store=ctx.store)
        except ParserUnavailable as e:
            raise TransientError("파서 컨테이너를 쓸 수 없음") from e
        # 파싱 작업 키(받은 첨부 수 포함)를 이어 쓴다. 첨부가 늦게 채워지면 비교도 다시 한다.
        enqueue(
            session,
            DIFF,
            {"program_id": pv.program_id},
            dedupe_key=f"{DIFF}:{job.dedupe_key}:{MATCHER_VERSION}",
            concurrency_key=f"program:{pv.program_id}",
            priority=60,
        )

    return handle


def handle_diff(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        results = record_program(session, job.payload["program_id"], ctx.store)
        waiting = [r for r in results if r.status == "waiting"]
        if waiting:
            # 첨부가 아직 없거나 파싱 전이다. 기다렸다 다시 본다(횟수를 넘기면 dead로 운영자 확인).
            raise TransientError(
                f"v{waiting[0].version_no} 첨부 대기: {', '.join(waiting[0].waiting)}"
            )

    return handle


def handlers(ctx: Context) -> dict[str, Handler]:
    return {LOAD: handle_load(ctx), PARSE: handle_parse(ctx), DIFF: handle_diff(ctx)}
