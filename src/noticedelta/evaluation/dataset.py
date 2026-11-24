"""평가 이벤트: 사업 하나의 첫 버전과 그 뒤 정정들. DB의 변경 기록을 다시 흘려 만든다."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import ChangeEvent, LabelRow, Notice, NoticeVersion
from noticedelta.evaluation.metrics import Label
from noticedelta.history import replay
from noticedelta.matching import ChangeSet
from noticedelta.segments import Segment


@dataclass
class EvalEvent:
    event_id: str  # 이후 사업 버전 ID. 라벨(label.to_version_id)과 같은 값이다
    item_id: str
    old_segments: list[Segment]
    change: ChangeSet
    unknown: set[str] = field(default_factory=set)  # 새 버전에서 읽지 못한 첨부의 조각
    meta_before: dict[str, Any] = field(default_factory=dict)  # API의 구조화 필드 (B0가 본다)
    meta_after: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chain:
    item_id: str
    first_segments: list[Segment]  # 첫 버전. 방식마다 여기서 처음 상태를 만든다
    events: list[EvalEvent]


def split_of(item_id: str) -> str:
    """사업 ID로 개발·시험을 가른다. 같은 사업의 정정은 한쪽에만 들어간다.

    시험 세트는 측정 동결 때만 본다. 프롬프트와 규칙은 개발 세트만 보고 고친다.
    """
    return "dev" if int(hashlib.sha256(item_id.encode()).hexdigest(), 16) % 2 == 0 else "test"


# (출처 이름, 원본 항목) → 출처 API가 구조화해 주는 값. 도메인 팩이 넘긴다 (B0가 본다)
MetaReader = Callable[[str, dict], dict[str, Any]]


def _meta(session: Session, notice_version_id: str, meta: MetaReader | None) -> dict[str, Any]:
    if meta is None:
        return {}
    row = session.execute(
        sa.select(Notice.source_id, NoticeVersion.raw)
        .join(Notice, Notice.id == NoticeVersion.notice_id)
        .where(NoticeVersion.id == notice_version_id)
    ).first()
    return meta(row[0], row[1]) if row else {}


def load_chains(
    session: Session,
    *,
    split: str | None = None,
    only_changed: bool = True,
    meta: MetaReader | None = None,
) -> list[Chain]:
    """정정이 한 번이라도 있는 사업의 흐름. only_changed면 조각이 실제로 바뀐 정정만 남긴다."""
    item_ids = session.scalars(
        sa.select(ChangeEvent.item_id)
        .where(ChangeEvent.from_version_id.is_not(None))
        .distinct()
        .order_by(ChangeEvent.item_id)
    ).all()
    chains = []
    for item_id in item_ids:
        if split is not None and split_of(item_id) != split:
            continue
        first: list[Segment] = []
        events = []
        for step in replay(session, item_id):
            if step.from_version is None:
                first = step.change.segments
                continue
            if only_changed and step.change.is_empty:
                continue
            events.append(
                EvalEvent(
                    event_id=step.to_version.id,
                    item_id=item_id,
                    old_segments=step.old_segments,
                    change=step.change,
                    unknown=step.unknown,
                    meta_before=_meta(session, step.from_version.notice_version_id, meta),
                    meta_after=_meta(session, step.to_version.notice_version_id, meta),
                )
            )
        if events:
            chains.append(Chain(item_id, first, events))
    return chains


def load_labels(session: Session, *, round_no: int = 1, labeler: str = "me") -> dict:
    """{(이벤트 ID, 필드): Label}. 정답은 사람 라벨뿐이다(DESIGN 7.7)."""
    rows = session.scalars(
        sa.select(LabelRow).where(LabelRow.round == round_no, LabelRow.labeler == labeler)
    )
    return {(r.to_version_id, r.field): Label(r.status, r.visible_in) for r in rows}
