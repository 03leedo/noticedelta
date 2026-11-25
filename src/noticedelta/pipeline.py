"""작업 종류로 이은 파이프라인 (설계 5장, 8장, 작업 목록 13번).

    적재(load_run) → 파싱(parse) → 변경 기록(diff) → 영향 계산(impact) → 알림(notify)

영향 계산은 추출기(LLM)가 설정돼 있을 때만 등록한다. 첫 버전은 필드를 모두 뽑고, 다음 버전부터는
바뀐 조각에 걸린 필드만 다시 뽑아 진짜 변경·재판정·알림(아웃박스)까지 쓴다.

각 핸들러는 자기 단계의 결과와 다음 작업 등록을 한 트랜잭션에 쓴다(run_worker가 커밋). 같은 일은
dedupe_key로 한 번만 등록되고, 사업 하나의 작업은 concurrency_key로 하나씩만 돈다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import app_registry, flight
from noticedelta.appspec import AppDefinition
from noticedelta.blobstore import BlobStore
from noticedelta.db import repo
from noticedelta.db.models import (
    Attachment,
    ChangeEvent,
    FieldValueRow,
    ItemVersion,
    Job,
    JudgmentRow,
    LoadedRun,
    OutboxAlert,
    Profile,
    SubscriptionRow,
)
from noticedelta.eligibility import Judgment, RuleGroup, judge, ruleset_hash
from noticedelta.events import Ref, Trace, app_ref, emit, subscribe
from noticedelta.events import Subscription as EventSubscription
from noticedelta.grants import APP, MASS_IMPACT, MIN_CONFIDENCE, RULES_FIELD, candidates
from noticedelta.history import MATCHER_VERSION, is_empty, record_item, replay
from noticedelta.impact import (
    Extractor,
    FieldValue,
    Subscription,
    run_impact,
)
from noticedelta.ingest import parse_attachments
from noticedelta.jobs import Handler, PermanentError, TransientError, enqueue
from noticedelta.llm.extract import FIELD_TYPES, TEMPLATE_VERSION
from noticedelta.names import ALERT_LABELS
from noticedelta.notify import Channel, InboxChannel, deliver, note, prepare
from noticedelta.parsing import Parser, ParserUnavailable
from noticedelta.programs import LoadSummary, load_run
from noticedelta.sources import CATALOG

LOAD, PARSE, DIFF, IMPACT, NOTIFY = "load_run", "parse", "diff", "impact", "notify"
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
    channel: Channel = field(default_factory=InboxChannel)
    labels: Mapping[str, str] = field(default_factory=lambda: dict(ALERT_LABELS))  # 알림 표시 이름
    app: AppDefinition = field(
        default_factory=lambda: APP
    )  # 결과(필드 값, 판정, 알림)에 이 앱의 id와 판을 단다


def parser_id(parser: Parser) -> str:
    """설정 해시에 넣을 파서의 판. 감싼 것은 벗긴다."""
    inner = getattr(parser, "inner", parser)
    return getattr(inner, "version", None) or type(inner).__name__


def flight_config(ctx: Context) -> dict:
    """수집 실행 묶음의 설정: 이것이 같아야 같게 재생을 기대한다(platform-services 3.4)."""
    return {
        "app": app_ref(ctx.app.id, ctx.app.version),
        "matcher": MATCHER_VERSION,
        "parser": parser_id(ctx.parser),
        "extractor": ctx.extractor_name or None,
    }


def _parse_time(value: str | None) -> datetime | None:
    try:
        t = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=UTC)


def _unloaded_runs(session: Session, root: Path) -> list[str]:
    loaded = set(session.scalars(sa.select(LoadedRun.name)).all())
    return [p.stem for p in sorted((root / "runs").glob("*.json")) if p.stem not in loaded]


def enqueue_load(session: Session, root: Path, now: datetime | None = None) -> Job | None:
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
        now=now,
    )


def _enqueue_parse(
    session: Session, pv_id: str, trace: Trace | None = None, now: datetime | None = None
) -> None:
    pv = session.get(ItemVersion, pv_id)
    blobs = session.scalar(
        sa.select(sa.func.count()).where(
            Attachment.notice_version_id == pv.notice_version_id, Attachment.blob_key.is_not(None)
        )
    )
    # 첨부가 늦게 채워지면 받은 첨부 수가 달라져 새 작업이 된다.
    enqueue(
        session,
        PARSE,
        {"item_version_id": pv_id, **(trace or Trace()).carry()},
        dedupe_key=f"{PARSE}:{pv_id}:{blobs}",
        concurrency_key=f"item:{pv.item_id}",
        priority=50,
        now=now,
    )


def enqueue_backfill(session: Session) -> int:
    """변경 기록이 없는 사업 버전을 모두 파싱·비교 작업으로 등록한다 (작업 큐 이전에 적재한 것)."""
    from noticedelta.db.models import ChangeEvent

    recorded = sa.select(ChangeEvent.to_version_id)
    ids = session.scalars(
        sa.select(ItemVersion.id)
        .where(ItemVersion.id.not_in(recorded))
        .order_by(ItemVersion.item_id, ItemVersion.version_no)
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
        run = json.loads(path.read_text(encoding="utf-8"))
        env = emit(
            session,
            "source.run_finished",
            {
                "run": pending[0],
                "sources": {
                    sid: {
                        "count": r.get("count"),
                        "changed": r.get("changed"),
                        "error": r.get("error"),
                    }
                    for sid, r in (run.get("sources") or {}).items()
                },
            },
            producer="svc:loader",
            key=pending[0],
            occurred_at=_parse_time(run.get("finished_at")),
            now=ctx.now(),
        )
        trace = Trace(env.trace_id if env else None, env.id if env else None)
        if env is not None:
            flight.open_trace(
                session,
                env.trace_id,
                kind=flight.COLLECT_RUN,
                name=pending[0],
                started_at=_parse_time(run.get("started_at")) or ctx.now(),
                config=flight_config(ctx),
                app=app_ref(ctx.app.id, ctx.app.version),
            )
        for pv_id in summary.new_version_ids:
            pv = session.get(ItemVersion, pv_id)
            v = emit(
                session,
                "document.version_created",
                {
                    "item_version_id": pv.id,
                    "notice_version_id": pv.notice_version_id,
                    "content_hash": pv.content_hash,
                },
                producer="svc:loader",
                key=pv.id,
                ref=Ref(item=f"item:{pv.item_id}", item_version=pv.version_no),
                trace=trace,
                now=ctx.now(),
            )
            _enqueue_parse(
                session, pv_id, Trace(trace.trace, v.id if v else trace.cause), ctx.now()
            )
        for pv_id in summary.refilled_version_ids:
            _enqueue_parse(session, pv_id, trace, ctx.now())
        enqueue_load(session, ctx.root, ctx.now())  # 남은 실행이 있으면 다음 것

    return handle


def handle_parse(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        pv = session.get(ItemVersion, job.payload["item_version_id"])
        if pv is None:
            raise PermanentError("사업 버전이 없음")
        try:
            with flight.bound(session, Trace.of(job.payload).trace, ctx.now):
                outcomes = parse_attachments(
                    session,
                    pv.notice_version_id,
                    parser=flight.RecordingParser(ctx.parser),
                    store=ctx.store,
                )
        except ParserUnavailable as e:
            raise TransientError("파서 컨테이너를 쓸 수 없음") from e
        env = emit(
            session,
            "document.parsed",
            {
                "item_version_id": pv.id,
                "attachments": len(outcomes),
                "unreadable": sum(o.status != "ok" for o in outcomes),
            },
            producer="svc:parser",
            key=job.dedupe_key,
            ref=Ref(item=f"item:{pv.item_id}", item_version=pv.version_no),
            trace=Trace.of(job.payload),
            now=ctx.now(),
        )
        # 파싱 작업 키(받은 첨부 수 포함)를 이어 쓴다. 첨부가 늦게 채워지면 비교도 다시 한다.
        enqueue(
            session,
            DIFF,
            {"item_id": pv.item_id, **Trace.of(job.payload).carry(env)},
            dedupe_key=f"{DIFF}:{job.dedupe_key}:{MATCHER_VERSION}",
            concurrency_key=f"item:{pv.item_id}",
            priority=60,
            now=ctx.now(),
        )

    return handle


def handle_diff(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        item_id = job.payload["item_id"]
        results = record_item(session, item_id, ctx.store, sheets=CATALOG)
        trace = Trace.of(job.payload)
        for r in results:
            if r.status != "recorded":
                continue
            c = r.event.changed_lineages
            env = emit(
                session,
                "change.detected",
                {
                    "from_version_id": r.event.from_version_id,
                    "to_version_id": r.event.to_version_id,
                    "added": len(c.get("added", [])),
                    "removed": len(c.get("removed", [])),
                    "modified": len(c.get("modified", [])),
                    "matcher": r.event.matcher_version,
                    "keys": c.get("keys"),
                    "attachments": bool(c.get("attachments")),
                },
                producer="svc:engine",
                key=r.event.to_version_id,
                ref=Ref(item=f"item:{item_id}", item_version=r.version_no),
                trace=trace,
                now=ctx.now(),
            )
            if ctx.extractor is not None:
                _enqueue_impact(
                    session, item_id, r.event.to_version_id, trace.carry(env), ctx.now()
                )
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
            {"item_id": item_id, "wait": n + 1, "reason": reason, **trace.carry()},
            dedupe_key=f"{job.dedupe_key}|w{n + 1}",
            concurrency_key=job.concurrency_key,
            priority=job.priority,
            run_after=ctx.now() + DIFF_WAIT_BASE * 2**n,
        )

    return handle


def _for_app(app: AppDefinition, event: ChangeEvent) -> bool:
    """이 앱에게 변경인가 (app-spec 9장, platform-services 8장의 3번).

    첫 판은 늘 변경이다(필드를 처음 뽑는다). 그 뒤는 조각이 바뀌었고, 바뀐 원본 키가 앱이 읽는
    키와 겹칠 때만. 아니면 영향 계산이 LLM을 부르지 않고 앞 판의 값을 그대로 넘긴다.
    """
    if event.from_version_id is None:
        return True
    if is_empty(event):
        return False
    c = event.changed_lineages
    source = c.get("source")
    if source is None:  # 출처를 모르면(예전 기록) 받는다
        return True
    return app.reads_change(source, c.get("keys"), bool(c.get("attachments")))


# --- 영향 계산 (설계 7.7) ---


def _impact_key(pv_id: str) -> str:
    return f"{IMPACT}:{pv_id}:{TEMPLATE_VERSION}"


def _enqueue_impact(
    session: Session,
    item_id: str,
    pv_id: str,
    carry: dict[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    enqueue(
        session,
        IMPACT,
        {"item_version_id": pv_id, **(carry or {})},
        dedupe_key=_impact_key(pv_id),
        concurrency_key=f"item:{item_id}",
        priority=70,
        now=now,
    )


def _load_fields(session: Session, pv_id: str, app_id: str) -> dict[str, FieldValue]:
    rows = session.scalars(
        sa.select(FieldValueRow).where(
            FieldValueRow.item_version_id == pv_id, FieldValueRow.app_id == app_id
        )
    ).all()
    return {
        r.name: FieldValue(r.name, r.value_type, r.value, list(r.source_lineage_ids), r.confidence)
        for r in rows
    }


def _save_fields(
    ctx: Context,
    session: Session,
    pv: ItemVersion,
    fields: dict[str, FieldValue],
    stale: set[str],
) -> None:
    for name, fv in fields.items():
        if name in stale:
            status = "stale"
        elif fv.confidence < MIN_CONFIDENCE:  # 앱 정의의 alerts.hold
            status = "needs_review"
        else:
            status = "verified"
        session.add(
            FieldValueRow(
                item_id=pv.item_id,
                item_version_id=pv.id,
                name=name,
                value=fv.value,
                value_type=fv.value_type,
                source_lineage_ids=list(fv.sources),
                confidence=fv.confidence,
                status=status,
                extractor=ctx.extractor_name,
                template_version=TEMPLATE_VERSION,
                app_id=ctx.app.id,
                app_version=ctx.app.version,
            )
        )


def _carry_fields(session: Session, from_pv_id: str, pv: ItemVersion, app_id: str) -> None:
    """앞 판의 필드 값을 상태(확인 필요·stale 포함)까지 그대로 이 판으로 옮긴다."""
    rows = session.scalars(
        sa.select(FieldValueRow).where(
            FieldValueRow.item_version_id == from_pv_id, FieldValueRow.app_id == app_id
        )
    ).all()
    skip = {"id", "item_version_id", "created_at"}
    for r in rows:
        cols = {
            c.key: getattr(r, c.key) for c in FieldValueRow.__table__.columns if c.key not in skip
        }
        session.add(FieldValueRow(**{**cols, "item_version_id": pv.id}))


def _latest_judgments(session: Session, item_id: str, app_id: str) -> dict[str, Judgment]:
    rows = session.scalars(
        sa.select(JudgmentRow)
        .where(JudgmentRow.item_id == item_id, JudgmentRow.app_id == app_id)
        .order_by(JudgmentRow.created_at)
    ).all()
    return {r.person_id: Judgment(r.result) for r in rows}  # 뒤의 것이 앞의 것을 덮는다


def handle_impact(ctx: Context) -> Handler:
    def _work(session: Session, job: Job) -> None:
        if ctx.extractor is None:
            raise TransientError("추출기가 설정되지 않음")
        pv = session.get(ItemVersion, job.payload["item_version_id"])
        step = next((s for s in replay(session, pv.item_id) if s.to_version.id == pv.id), None)
        if step is None:
            raise PermanentError("변경 이벤트가 없음")
        app_registry.seed(session, ctx.app, at=ctx.now())  # 결과가 가리킬 앱 정의가 DB에 있게

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
        event = session.scalar(sa.select(ChangeEvent).where(ChangeEvent.to_version_id == pv.id))
        if not _for_app(ctx.app, event):
            # 이 앱이 읽지 않는 것만 바뀌었다. 다음 판이 이어 받도록 값만 옮긴다(LLM·알림 없음)
            _carry_fields(session, step.from_version.id, pv, ctx.app.id)
            return

        item_id = pv.item_id
        subs = [
            Subscription(r.person_id, set(r.fields))
            for r in session.scalars(
                sa.select(SubscriptionRow).where(
                    SubscriptionRow.item_id == item_id, SubscriptionRow.app_id == ctx.app.id
                )
            )
        ]
        old_judgments = _latest_judgments(session, item_id, ctx.app.id)
        users = {s.person_id for s in subs} | set(old_judgments)
        profiles = {
            p.person_id: dict(p.attributes)
            for p in session.scalars(sa.select(Profile).where(Profile.person_id.in_(users)))
        }
        res = run_impact(
            candidate_detector=candidates,
            item_id=item_id,
            to_version_id=pv.id,
            change=step.change,
            old_fields=_load_fields(session, step.from_version.id, ctx.app.id),
            extractor=ctx.extractor,
            subscriptions=subs,
            profiles=profiles,
            old_judgments=old_judgments,
            old_segments=step.old_segments,
            unknown_lineages=step.unknown,
            app_id=ctx.app.id,
            app_version=ctx.app.version,
        )
        _save_fields(ctx, session, pv, res.fields, res.stale)
        trace = Trace.of(job.payload)
        app = app_ref(ctx.app.id, ctx.app.version)
        where = Ref(app=app, item=f"item:{item_id}", item_version=pv.version_no)
        for name in sorted(res.real_changes):
            fv = res.fields.get(name)
            if fv is None:
                continue
            emit(
                session,
                "field.changed",
                {
                    "field": name,
                    "value_type": fv.value_type,
                    "to_version_id": pv.id,
                    "sources": list(fv.sources),
                    "confidence": fv.confidence,
                    "after": _short(fv.value),
                },
                producer="svc:engine",
                key=f"{ctx.app.id}:{pv.id}:{name}",
                ref=Ref(
                    app=app,
                    item=where.item,
                    item_version=where.item_version,
                    labels={"llm_derived": True, "confidence": fv.confidence},
                ),
                trace=trace,
                now=ctx.now(),
            )

        if RULES_FIELD in res.real_changes:
            rules = RuleGroup.model_validate(res.fields[RULES_FIELD].value)
            h = ruleset_hash(rules)
            for person_id, attrs in profiles.items():
                j = judge(rules, attrs)
                session.add(
                    JudgmentRow(
                        person_id=person_id,
                        item_id=item_id,
                        item_version_id=pv.id,
                        ruleset_hash=h,
                        result=j.result,
                        reasons=[vars(r) for r in j.reasons],
                        app_id=ctx.app.id,
                        app_version=ctx.app.version,
                    )
                )
        session.flush()
        for alert in res.alerts:
            person = Ref(
                app=app,
                subject=f"person:{alert.person_id}",
                item=where.item,
                item_version=where.item_version,
            )
            cause = None
            if alert.kind == "judgment_changed":
                cause = emit(
                    session,
                    "verdict.changed",
                    {
                        "field": alert.field,
                        "to_version_id": pv.id,
                        "before": alert.before,
                        "after": alert.after,
                        "evidence": [f"lineage:{s}" for s in alert.sources],
                    },
                    producer="svc:engine",
                    key=alert.idempotency_key,
                    ref=person,
                    trace=trace,
                    now=ctx.now(),
                )
            if not repo.enqueue_alert(session, alert):
                continue
            row = session.scalar(
                sa.select(OutboxAlert).where(OutboxAlert.idempotency_key == alert.idempotency_key)
            )
            # 알림이 생겼다는 봉투를 알림 발송이 구독한다(아래 NOTIFY_ON_ALERT)
            note(
                session,
                row,
                "created",
                trace=Trace(trace.trace, cause.id if cause else trace.cause),
                producer="svc:engine",
            )
            if alert.needs_review:
                # 근거 확인을 통과하지 못한 값은 바로 보내지 않고 붙잡는다 (설계 7.8).
                row.status = "held"
                row.held_reason = f"근거 확인 필요 (신뢰도 {alert.confidence:.2f})"
                note(
                    session, row, "held", reason=row.held_reason, trace=trace, producer="svc:engine"
                )

    def handle(session: Session, job: Job) -> None:
        # 추출기의 게이트웨이에 flight.llm_observer를 달면 이 작업의 LLM 호출이 묶음에 기록된다
        with flight.bound(session, Trace.of(job.payload).trace, ctx.now):
            _work(session, job)

    return handle


def _short(value, limit: int = 500):
    """봉투에 넣을 짧은 값. 길면 넣지 않는다(원문 노출 한도, 64KB 한도)."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else None


# 알림 발송은 알림이 생겼다는 봉투를 받아 돈다(platform-services 2.5). 항목 판 하나에 한 번.
NOTIFY_ON_ALERT = EventSubscription(
    name="notify",
    pattern="alert.created",
    job=NOTIFY,
    dedupe=lambda env: f"{NOTIFY}:{env.payload['to_version_id']}",
    concurrency=lambda env: env.item,
    payload=lambda env: {"item_id": (env.item or "").removeprefix("item:")},
    priority=80,
)
subscribe(NOTIFY_ON_ALERT)


def handle_notify(ctx: Context) -> Handler:
    def handle(session: Session, job: Job) -> None:
        item_id = job.payload["item_id"]
        trace = Trace.of(job.payload)
        prepare(session, item_id, mass_impact=MASS_IMPACT, trace=trace)
        deliver(session, ctx.channel, item_id=item_id, now=ctx.now, labels=ctx.labels, trace=trace)

    return handle


def handlers(ctx: Context) -> dict[str, Handler]:
    return {
        LOAD: handle_load(ctx),
        PARSE: handle_parse(ctx),
        DIFF: handle_diff(ctx),
        IMPACT: handle_impact(ctx),
        NOTIFY: handle_notify(ctx),
    }
