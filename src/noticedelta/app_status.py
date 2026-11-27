"""앱 상태 보고 (app-spec 11장): 앱마다 지표를 계산하고 소유자에게 사람 말로 요약한다.

    s = app_status(session, app, root=스냅샷 폴더, since=7일 전, now=지금, primary=주 앱, daily=하루 한도)
    for line in describe(s): print(line)

| 지표 | 어디서 |
|---|---|
| 수집 성공·빈틈 | 수집 실행 파일(runs/*.json): 출처별 성공·실패, 첨부 받기 실패, 실행 사이의 가장 긴 틈 |
| 형식 변화 | 붙잡아 둔 키(drift.pending) 가운데 앱이 읽는 출처 |
| 파싱 실패·확인 필요 | 기간 안에 받은 판의 첨부 파싱 상태, 앱 필드 값의 확인 필요 비율 |
| 붙잡힌 알림 | 앱의 알림 가운데 held, 기간 안에 나간 알림 |
| 예산 | 오늘 쓴 토큰과 앱의 하루 한도, 기간 안에 쓴 토큰 |

시험(맞다고 표시한 알림 중 지금 판에서 깨진 것)은 미리 돌려 보기(preview_app, scripts/backtest.py)가 낸다.
개인정보는 없다(사람 수만 센다).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import app_registry, budgets, drift, flight
from noticedelta.appspec import AppDefinition
from noticedelta.db.models import (
    Attachment,
    FieldValueRow,
    ItemVersion,
    Notice,
    NoticeVersion,
    OutboxAlert,
)

GAP_WARN = timedelta(hours=12)  # 수집 실행 사이가 이보다 길면 빈틈으로 본다(6시간마다 돈다)


def _local(t: datetime) -> datetime:
    """실행 파일의 시각은 시간대 없는 로컬 시각이다. 비교하려고 맞춘다."""
    return t.astimezone().replace(tzinfo=None) if t.tzinfo is not None else t


def _runs(root: Path, since: datetime, now: datetime, sources: list[str]) -> dict:
    lo, hi = _local(since), _local(now)
    times, per = [], {s: Counter() for s in sources}
    for path in sorted((root / "runs").glob("*.json")):
        try:
            at = datetime.strptime(path.stem, "%Y%m%dT%H%M%S")
        except ValueError:
            continue
        if not lo <= at <= hi:
            continue
        times.append(at)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for src in sources:
            r = (data.get("sources") or {}).get(src)
            if r is None:
                continue
            c = per[src]
            c["runs"] += 1
            c["failed"] += bool(r.get("error"))
            c["attachment_errors"] += int(r.get("attachment_errors") or 0)
            c["changed"] += int(r.get("changed") or 0)
    edges = [lo, *times, hi]
    gap = max((b - a for a, b in zip(edges, edges[1:], strict=False)), default=timedelta(0))
    return {
        "runs": len(times),
        "longest_gap_hours": round(gap.total_seconds() / 3600, 1),
        "sources": {s: dict(c) for s, c in per.items()},
    }


def app_status(
    session: Session,
    app: AppDefinition,
    *,
    root: Path,
    since: datetime,
    now: datetime,
    primary: str,
    daily: int,
    today: datetime,
) -> dict:
    """today: 오늘 0시(하루 한도를 세는 시작)."""
    sources = [u.id for u in app.sources]
    held_keys = [f"{d.source}.{d.key}" for d in drift.pending(session) if d.source in sources]
    recent = (
        sa.select(NoticeVersion.id)
        .join(Notice, Notice.id == NoticeVersion.notice_id)
        .where(Notice.source_id.in_(sources), NoticeVersion.fetched_at >= since)
    )
    parse = Counter(
        session.scalars(
            sa.select(Attachment.parse_status).where(Attachment.notice_version_id.in_(recent))
        ).all()
    )
    fields = Counter(
        session.scalars(
            sa.select(FieldValueRow.status)
            .join(ItemVersion, ItemVersion.id == FieldValueRow.item_version_id)
            .where(FieldValueRow.app_id == app.id, ItemVersion.notice_version_id.in_(recent))
        ).all()
    )
    alerts = session.execute(
        sa.select(OutboxAlert.status, OutboxAlert.sent_at).where(OutboxAlert.app_id == app.id)
    ).all()
    held = sum(1 for st, _ in alerts if st == "held")
    sent = sum(
        1
        for st, at in alerts
        if st in ("sent", "corrected") and at is not None and _naive(at) >= _naive(since)
    )
    ref = [app_registry.usage_ref(app.id, primary)]
    return {
        "app": f"{app.id}@{app.version}",
        "since": since.isoformat(timespec="minutes"),
        "collect": _runs(root, since, now, sources),
        "drift_held": held_keys,
        "attachments": dict(parse),
        "fields": dict(fields),
        "alerts": {"held": held, "sent": sent},
        "budget": {
            "daily": budgets.cap_of(session, app.id, primary=primary, daily=daily),
            "today": flight.spent_tokens(session, today, refs=ref),
            "period": flight.spent_tokens(session, since, refs=ref),
        },
    }


def _naive(t: datetime) -> datetime:
    return t.replace(tzinfo=None) if t.tzinfo is not None else t


def describe(s: dict) -> list[str]:
    """소유자에게 보일 사람 말 요약. 문제가 없으면 한 줄로 그렇다고 말한다."""
    lines: list[str] = []
    c = s["collect"]
    failed = {src: v.get("failed", 0) for src, v in c["sources"].items() if v.get("failed")}
    if c["runs"] == 0:
        lines.append("이 기간에 수집이 한 번도 돌지 않았습니다.")
    elif failed:
        what = ", ".join(f"{src} {n}번" for src, n in failed.items())
        lines.append(f"수집 {c['runs']}번 중 출처가 멈춘 적이 있습니다({what}).")
    else:
        lines.append(f"수집 {c['runs']}번이 모두 제대로 돌았습니다.")
    if c["longest_gap_hours"] > GAP_WARN.total_seconds() / 3600:
        lines.append(f"수집 사이가 가장 길게는 {c['longest_gap_hours']}시간 비었습니다.")
    att = sum(v.get("attachment_errors", 0) for v in c["sources"].values())
    if att:
        lines.append(f"첨부를 받지 못한 적이 {att}번 있습니다(다음 수집에서 다시 받습니다).")
    if s["drift_held"]:
        lines.append(
            f"출처의 형식이 바뀐 것으로 보여 붙잡아 둔 키가 있습니다: {', '.join(s['drift_held'])}."
        )
    a = s["attachments"]
    total = sum(a.values())
    bad = a.get("needs_review", 0) + a.get("unsupported", 0)
    if total and bad:
        lines.append(f"새 첨부 {total}개 중 {bad}개는 읽지 못했거나 확인이 필요합니다.")
    f = s["fields"]
    if sum(f.values()) and f.get("needs_review"):
        lines.append(
            f"뽑은 값 {sum(f.values())}개 중 {f['needs_review']}개는 근거가 약해 확인이 필요합니다."
        )
    if s["alerts"]["held"]:
        lines.append(f"붙잡혀 확인을 기다리는 알림이 {s['alerts']['held']}개 있습니다.")
    lines.append(f"이 기간에 나간 알림은 {s['alerts']['sent']}개입니다.")
    b = s["budget"]
    lines.append(
        f"오늘 LLM 토큰 {b['today']:,} / 하루 한도 {b['daily']:,}(이 기간 {b['period']:,})."
    )
    if b["daily"] and b["today"] >= b["daily"]:
        lines.append("오늘 한도를 다 써서 새 정정의 영향 계산은 내일로 미뤄집니다.")
    return lines
