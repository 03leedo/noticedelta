"""공고 상세·변경 이력·알림 근거 (작업 목록 16번). 기록된 변경 이벤트를 재생해 보여 준다.

엔진이 남긴 근거 체인(조각 → 필드 → 판정 → 알림)을 화면에서 거꾸로 따라간다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import (
    FieldValueRow,
    Notice,
    OutboxAlert,
    Program,
    ProgramVersion,
    SegmentRow,
)
from noticedelta.history import ReplayStep, replay

MAX_PAIRS = 30  # 한 정정에서 보여 줄 바뀐 조각 수 (공고문이 통째로 바뀌면 수백 개다)
MAX_TEXT = 300


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


@dataclass
class Pair:
    kind: str  # modified / added / removed
    before: str | None
    after: str | None
    where: str  # 본문 / 첨부 파일 이름


@dataclass
class Correction:
    from_no: int | None
    to_no: int
    seen_at: datetime | None
    total: int
    added: int
    removed: int
    modified: int
    unknown: int
    pairs: list[Pair]
    hidden: int  # 다 보여 주지 못한 쌍 수


@dataclass
class ProgramDetail:
    program: Program
    postings: list[Notice]
    fields: dict[str, FieldValueRow]
    corrections: list[Correction] = field(default_factory=list)


def _where(locator: dict | None) -> str:
    if not locator or locator.get("source") == "body":
        return "본문"
    return locator.get("filename") or "첨부"


def _pairs(step: ReplayStep, limit: int) -> tuple[list[Pair], int]:
    out = []
    for old, new in step.pairs():
        kind = "modified" if old and new else "added" if new else "removed"
        loc = (new or old).locator
        out.append(
            Pair(
                kind,
                _clip(old.text if old else None),
                _clip(new.text if new else None),
                _where(loc),
            )
        )
    return out[:limit], max(0, len(out) - limit)


def _correction(step: ReplayStep, limit: int = MAX_PAIRS) -> Correction:
    c = step.change
    pairs, hidden = _pairs(step, limit)
    return Correction(
        step.from_version.version_no if step.from_version else None,
        step.to_version.version_no,
        step.to_version.source_time,
        step.total,
        len(c.added),
        len(c.removed),
        len(c.modified),
        len(step.unknown),
        pairs,
        hidden,
    )


def program_detail(session: Session, program_id: str) -> ProgramDetail | None:
    program = session.get(Program, program_id)
    if program is None:
        return None
    postings = session.scalars(
        sa.select(Notice).where(Notice.program_id == program_id).order_by(Notice.first_seen_at)
    ).all()
    latest = session.scalar(
        sa.select(ProgramVersion.id)
        .join(FieldValueRow, FieldValueRow.program_version_id == ProgramVersion.id)
        .where(ProgramVersion.program_id == program_id)
        .order_by(ProgramVersion.version_no.desc())
    )
    fields = {}
    if latest is not None:
        fields = {
            r.name: r
            for r in session.scalars(
                sa.select(FieldValueRow).where(FieldValueRow.program_version_id == latest)
            )
        }
    corrections = [_correction(s) for s in replay(session, program_id) if s.from_version]
    corrections.reverse()  # 최근 정정부터
    return ProgramDetail(program, postings, fields, corrections)


# --- 알림 근거 ---


@dataclass
class Evidence:
    alert: OutboxAlert
    title: str
    quotes: list[str]  # 알림이 근거로 든 새 버전의 원문 조각
    pairs: list[Pair]  # 그 조각이 이번 정정에서 어떻게 바뀌었나


def alert_evidence(session: Session, alert_id: str, user_id: str) -> Evidence | None:
    alert = session.get(OutboxAlert, alert_id)
    if alert is None or alert.user_id != user_id:
        return None  # 남의 알림은 없는 것과 같다
    p = alert.payload
    to_version = session.get(ProgramVersion, p.get("to_version_id"))
    program = session.get(Program, alert.program_id)
    sources = set(p.get("sources") or [])
    quotes, pairs = [], []
    if to_version is not None and sources:
        quotes = [
            t
            for (t,) in session.execute(
                sa.select(SegmentRow.text)
                .where(
                    SegmentRow.notice_version_id == to_version.notice_version_id,
                    SegmentRow.lineage_id.in_(sources),
                )
                .order_by(SegmentRow.ord)
            )
        ]
        step = next(
            (s for s in replay(session, alert.program_id) if s.to_version.id == to_version.id), None
        )
        if step is not None:
            pairs = [
                pair
                for (old, new), pair in zip(step.pairs(), _pairs(step, 10_000)[0], strict=True)
                if (new and new.lineage_id in sources) or (old and old.lineage_id in sources)
            ]
    return Evidence(alert, program.title if program else alert.program_id, quotes, pairs)
