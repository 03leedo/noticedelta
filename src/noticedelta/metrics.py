"""운영 지표 (설계 9.4, 작업 목록 20번): 단계별 대기 작업·처리 시간, 파싱 실패율, 정정 → 알림 지연.

DB만 읽는다. Prometheus 텍스트(`prometheus_text`)와 측정표용 JSON(`collect`) 두 모양으로 낸다.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import Attachment, ChangeEvent, Job, OutboxAlert, ProgramVersion


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)  # SQLite는 시간대를 지운다


def percentile(values: list[float], q: float) -> float | None:
    """가장 가까운 순위 방식의 백분위수. 값이 없으면 None."""
    if not values:
        return None
    xs = sorted(values)
    return xs[max(0, math.ceil(q * len(xs)) - 1)]


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def collect(
    session: Session,
    *,
    now: datetime | None = None,
    window: timedelta = timedelta(days=1),
    snapshots_root: Path | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - window

    jobs: dict[str, dict[str, int]] = defaultdict(dict)
    for kind, status, n in session.execute(
        sa.select(Job.kind, Job.status, sa.func.count()).group_by(Job.kind, Job.status)
    ):
        jobs[kind][status] = n

    oldest: dict[str, float] = {}
    for kind, t in session.execute(
        sa.select(Job.kind, sa.func.min(Job.run_after))
        .where(Job.status.in_(("queued", "retry")))
        .group_by(Job.kind)
    ):
        oldest[kind] = max(0.0, (now - _utc(t)).total_seconds())

    wait: dict[str, list[float]] = defaultdict(list)
    run: dict[str, list[float]] = defaultdict(list)
    for kind, created, started, finished in session.execute(
        sa.select(Job.kind, Job.created_at, Job.started_at, Job.finished_at).where(
            Job.status == "done", Job.finished_at >= since
        )
    ):
        if started is not None:
            wait[kind].append((_utc(started) - _utc(created)).total_seconds())
            run[kind].append((_utc(finished) - _utc(started)).total_seconds())

    parse = dict(
        session.execute(
            sa.select(Attachment.parse_status, sa.func.count()).group_by(Attachment.parse_status)
        ).all()
    )
    tried = sum(n for s, n in parse.items() if s != "pending")
    outbox = dict(
        session.execute(
            sa.select(OutboxAlert.status, sa.func.count()).group_by(OutboxAlert.status)
        ).all()
    )

    # 정정을 처음 본 시각(사업 버전의 수집 시각) → 알림을 보낸 시각
    latency = []
    for sent_at, payload in session.execute(
        sa.select(OutboxAlert.sent_at, OutboxAlert.payload).where(
            OutboxAlert.sent_at.is_not(None), OutboxAlert.sent_at >= since
        )
    ):
        seen = session.scalar(
            sa.select(ProgramVersion.source_time).where(
                ProgramVersion.id == payload.get("to_version_id")
            )
        )
        if seen is not None:
            latency.append((_utc(sent_at) - _utc(seen)).total_seconds())

    out: dict[str, Any] = {
        "at": now.isoformat(timespec="seconds"),
        "window_hours": window.total_seconds() / 3600,
        "jobs": dict(jobs),
        "queue_depth": {
            k: v.get("queued", 0) + v.get("retry", 0) + v.get("running", 0) for k, v in jobs.items()
        },
        "oldest_waiting_seconds": oldest,
        "wait_seconds": {k: _summary(v) for k, v in wait.items()},
        "run_seconds": {k: _summary(v) for k, v in run.items()},
        "parse": parse,
        "parse_failure_rate": (
            (parse.get("needs_review", 0) / tried) if tried else None
        ),  # 받았지만 읽지 못한 비율 (지원하지 않는 형식은 뺀다)
        "change_events": session.scalar(sa.select(sa.func.count()).select_from(ChangeEvent)),
        "outbox": outbox,
        "correction_to_alert_seconds": {
            **_summary(latency),
            "p99": percentile(latency, 0.99),
        },
    }
    if snapshots_root is not None and (snapshots_root / "last_run.json").exists():
        last = json.loads((snapshots_root / "last_run.json").read_text(encoding="utf-8"))
        out["last_collection"] = {"finished_at": last.get("finished_at"), "ok": last.get("ok")}
    return out


def _line(name: str, value: float | int | None, **labels: str) -> str | None:
    if value is None:
        return None
    lab = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{lab}}} {value}" if lab else f"{name} {value}"


def prometheus_text(m: dict[str, Any]) -> str:
    """Prometheus 텍스트 노출 형식."""
    lines = [
        "# HELP noticedelta_jobs 작업 수 (종류·상태별)",
        "# TYPE noticedelta_jobs gauge",
    ]
    for kind, by in sorted(m["jobs"].items()):
        lines += [_line("noticedelta_jobs", n, kind=kind, status=s) for s, n in sorted(by.items())]
    lines += ["# TYPE noticedelta_oldest_waiting_seconds gauge"]
    lines += [
        _line("noticedelta_oldest_waiting_seconds", round(v, 1), kind=k)
        for k, v in sorted(m["oldest_waiting_seconds"].items())
    ]
    for metric, key in (
        ("noticedelta_job_wait_seconds", "wait_seconds"),
        ("noticedelta_job_run_seconds", "run_seconds"),
    ):
        lines += [f"# TYPE {metric} gauge"]
        for kind, s in sorted(m[key].items()):
            lines += [_line(metric, s["p50"], kind=kind, quantile="0.5"), _line(metric, s["p95"], kind=kind, quantile="0.95")]  # fmt: skip
    lines += ["# TYPE noticedelta_attachments gauge"]
    lines += [_line("noticedelta_attachments", n, status=s) for s, n in sorted(m["parse"].items())]
    lines += ["# TYPE noticedelta_alerts gauge"]
    lines += [_line("noticedelta_alerts", n, status=s) for s, n in sorted(m["outbox"].items())]
    lat = m["correction_to_alert_seconds"]
    lines += ["# TYPE noticedelta_correction_to_alert_seconds gauge"]
    lines += [
        _line("noticedelta_correction_to_alert_seconds", lat[q], quantile=label)
        for q, label in (("p50", "0.5"), ("p95", "0.95"), ("p99", "0.99"))
    ]
    lines += [_line("noticedelta_change_events", m["change_events"])]
    return "\n".join(x for x in lines if x is not None) + "\n"
