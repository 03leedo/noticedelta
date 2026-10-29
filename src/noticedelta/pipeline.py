"""작업 종류로 이은 파이프라인 (설계 5장, 8장, 작업 목록 13번).

    적재(load_run) → 파싱(parse) → 변경 기록(diff) → 영향 계산(impact) → [알림 발송은 17번]

영향 계산은 추출기(LLM)가 설정돼 있을 때만 등록한다. 첫 버전은 필드를 모두 뽑고, 다음 버전부터는
바뀐 조각에 걸린 필드만 다시 뽑아 진짜 변경·재판정·알림(아웃박스)까지 쓴다.

각 핸들러는 자기 단계의 결과와 다음 작업 등록을 한 트랜잭션에 쓴다(run_worker가 커밋). 같은 일은
dedupe_key로 한 번만 등록되고, 사업 하나의 작업은 concurrency_key로 하나씩만 돈다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.blobstore import BlobStore
from noticedelta.db import repo
from noticedelta.db.models import (
    Attachment,
    FieldValueRow,
    Job,
    JudgmentRow,
    LoadedRun,
    OutboxAlert,
    Profile,
    ProgramVersion,
    SubscriptionRow,
)
from noticedelta.eligibility import Judgment, RuleGroup, judge, ruleset_hash
from noticedelta.history import MATCHER_VERSION, record_program, replay
from noticedelta.impact import (
    REVIEW_THRESHOLD,
    RULES_FIELD,
    Extractor,
    FieldValue,
    Subscription,
    run_impact,
)
from noticedelta.ingest import parse_attachments
from noticedelta.jobs import Handler, PermanentError, TransientError, enqueue
from noticedelta.llm.extract import FIELD_TYPES, TEMPLATE_VERSION
from noticedelta.parsing import Parser, ParserUnavailable
from noticedelta.programs import LoadSummary, load_run

LOAD, PARSE, DIFF, IMPACT = "load_run", "parse", "diff", "impact"
LOADER_KEY = "loader"  # 실행 파일은 시간 순으로 하나씩 넣는다
DIFF_WAIT_BASE = timedelta(minutes=30)  # 첨부를 기다리는 비교의 다음 시도 간격(두 배씩)
DIFF_MAX_WAITS = 6


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Context:
    root: Path  # 스냅샷 폴더 (runs/, blobs/)
    store: BlobStore
    parser: Parser
    extractor: Extractor | None = None  # 없으면 변경 기록에서 멈춘다
    extractor_name: str = ""
    now: Callable[[], datetime] = field(default=_utcnow)


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


def enqueue_backfill(session: Session) -> int:
    """변경 기록이 없는 사업 버전을 모두 파싱·비교 작업으로 등록한다 (작업 큐 이전에 적재한 것)."""
    from noticedelta.db.models import ChangeEvent

    recorded = sa.select(ChangeEvent.to_version_id)
    ids = session.scalars(
        sa.select(ProgramVersion.id)
        .where(ProgramVersion.id.not_in(recorded))
        .order_by(ProgramVersion.program_id, ProgramVersion.version_no)
    ).all()
    for pv_id in ids:
        _enqueue_parse(session, pv_id)
    return len(ids)


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
        program_id = job.payload["program_id"]
        results = record_program(session, program_id, ctx.store)
        if ctx.extractor is not None:
            for r in results:
                if r.status == "recorded":
                    _enqueue_impact(session, program_id, r.event.to_version_id)
        waiting = next((r for r in results if r.status == "waiting"), None)
        if waiting is None:
            return
        # 첨부가 아직 없거나 파싱 전이다. 앞 버전 기록은 이번에 커밋하고, 이 버전은 나중에 다시 본다.
        # 실패로 올리면 앞 버전 기록까지 되돌려지므로 지연된 새 작업을 등록한다.
        n = job.payload.get("wait", 0)
        reason = f"v{waiting.version_no} 첨부 대기: {', '.join(waiting.waiting)}"
        if n >= DIFF_MAX_WAITS:
            raise PermanentError(reason)
        enqueue(
            session,
            DIFF,
            {"program_id": program_id, "wait": n + 1, "reason": reason},
            dedupe_key=f"{job.dedupe_key}|w{n + 1}",
            concurrency_key=job.concurrency_key,
            priority=job.priority,
            run_after=ctx.now() + DIFF_WAIT_BASE * 2**n,
        )

    return handle


# --- 영향 계산 (설계 7.7) ---


def _impact_key(pv_id: str) -> str:
    return f"{IMPACT}:{pv_id}:{TEMPLATE_VERSION}"


def _enqueue_impact(session: Session, program_id: str, pv_id: str) -> None:
    enqueue(
        session,
        IMPACT,
        {"program_version_id": pv_id},
        dedupe_key=_impact_key(pv_id),
        concurrency_key=f"program:{program_id}",
        priority=70,
    )


def _load_fields(session: Session, pv_id: str) -> dict[str, FieldValue]:
    rows = session.scalars(
        sa.select(FieldValueRow).where(FieldValueRow.program_version_id == pv_id)
    ).all()
    return {
        r.name: FieldValue(r.name, r.value_type, r.value, list(r.source_lineage_ids), r.confidence)
        for r in rows
    }


def _save_fields(
    ctx: Context,
    session: Session,
    pv: ProgramVersion,
    fields: dict[str, FieldValue],
    stale: set[str],
) -> None:
    for name, fv in fields.items():
        if name in stale:
            status = "stale"
        elif fv.confidence < REVIEW_THRESHOLD:
            status = "needs_review"
        else:
            status = "verified"
        session.add(
            FieldValueRow(
                program_id=pv.program_id,
                program_version_id=pv.id,
                name=name,
                value=fv.value,
                value_type=fv.value_type,
                source_lineage_ids=list(fv.sources),
                confidence=fv.confidence,
                status=status,
                extractor=ctx.extractor_name,
                template_version=TEMPLATE_VERSION,
            )
        )


def _latest_judgments(session: Session, program_id: str) -> dict[str, Judgment]:
    rows = session.scalars(
        sa.select(JudgmentRow)
        .where(JudgmentRow.program_id == program_id)
        .order_by(JudgmentRow.created_at)
    ).all()
    return {r.user_id: Judgment(r.result) for r in rows}  # 뒤의 것이 앞의 것을 덮는다


def handle_impact(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        if ctx.extractor is None:
            raise TransientError("추출기가 설정되지 않음")
        pv = session.get(ProgramVersion, job.payload["program_version_id"])
        step = next((s for s in replay(session, pv.program_id) if s.to_version.id == pv.id), None)
        if step is None:
            raise PermanentError("변경 이벤트가 없음")

        if step.from_version is None:
            # 첫 버전: 모든 필드를 뽑는다. 새 사업이므로 알림은 없다.
            fields = ctx.extractor(set(FIELD_TYPES), step.change.segments)
            _save_fields(ctx, session, pv, fields, set())
            return

        prev = session.scalar(
            sa.select(Job.status).where(Job.dedupe_key == _impact_key(step.from_version.id))
        )
        if prev != "done":
            raise TransientError(f"v{step.from_version.version_no} 영향 계산이 아직 안 끝남")

        program_id = pv.program_id
        subs = [
            Subscription(r.user_id, set(r.fields))
            for r in session.scalars(
                sa.select(SubscriptionRow).where(SubscriptionRow.program_id == program_id)
            )
        ]
        old_judgments = _latest_judgments(session, program_id)
        users = {s.user_id for s in subs} | set(old_judgments)
        profiles = {
            p.user_id: dict(p.attributes)
            for p in session.scalars(sa.select(Profile).where(Profile.user_id.in_(users)))
        }
        res = run_impact(
            program_id=program_id,
            to_version_id=pv.id,
            change=step.change,
            old_fields=_load_fields(session, step.from_version.id),
            extractor=ctx.extractor,
            subscriptions=subs,
            profiles=profiles,
            old_judgments=old_judgments,
            old_segments=step.old_segments,
            unknown_lineages=step.unknown,
        )
        _save_fields(ctx, session, pv, res.fields, res.stale)

        if RULES_FIELD in res.real_changes:
            rules = RuleGroup.model_validate(res.fields[RULES_FIELD].value)
            h = ruleset_hash(rules)
            for user_id, attrs in profiles.items():
                j = judge(rules, attrs)
                session.add(
                    JudgmentRow(
                        user_id=user_id,
                        program_id=program_id,
                        program_version_id=pv.id,
                        ruleset_hash=h,
                        result=j.result,
                        reasons=[vars(r) for r in j.reasons],
                    )
                )
        session.flush()
        for alert in res.alerts:
            if repo.enqueue_alert(session, alert) and alert.needs_review:
                # 근거 확인을 통과하지 못한 값은 바로 보내지 않고 붙잡는다 (설계 7.8).
                row = session.scalar(
                    sa.select(OutboxAlert).where(
                        OutboxAlert.idempotency_key == alert.idempotency_key
                    )
                )
                row.status = "held"
                row.held_reason = f"근거 확인 필요 (신뢰도 {alert.confidence:.2f})"

    return handle


def handlers(ctx: Context) -> dict[str, Handler]:
    return {
        LOAD: handle_load(ctx),
        PARSE: handle_parse(ctx),
        DIFF: handle_diff(ctx),
        IMPACT: handle_impact(ctx),
    }
