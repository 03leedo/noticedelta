"""사업 버전 사이의 변경 이벤트와 버전 이력 (설계 6장 change_event, 7.4, 10장 재생).

사업 버전마다 조각(lineage_id 포함)과 변경 이벤트 하나를 남긴다. 이벤트는 바뀐 lineage 목록이고,
조각은 그 버전의 원문 조각이라, 둘만 있으면 매칭을 다시 돌리지 않고 같은 ChangeSet을 다시 만든다.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.blobstore import BlobStore
from noticedelta.db.models import ChangeEvent, NoticeVersion, ProgramVersion, SegmentRow
from noticedelta.ingest import version_segments
from noticedelta.matching import ChangeSet, match_segments
from noticedelta.programs import (
    attachments_settled,
    comparable_old_segments,
    unreadable_attachments,
)
from noticedelta.segments import Segment

MATCHER_VERSION = "match-v1"  # 매칭 규칙을 바꾸면 올린다. 다른 버전의 이벤트끼리는 비교하지 않는다.
CARRIED = "carried"  # 새 버전에서 읽지 못한 첨부의 옛 조각을 이어 둔 표시 (locator 키)


@dataclass
class Recorded:
    version_no: int
    status: Literal["recorded", "exists", "waiting"]
    event: ChangeEvent | None = None
    waiting: list[str] = field(default_factory=list)  # 아직 끝나지 않은 첨부 이름


def stored_segments(session: Session, notice_version_id: str) -> list[Segment]:
    rows = session.scalars(
        sa.select(SegmentRow)
        .where(SegmentRow.notice_version_id == notice_version_id)
        .order_by(SegmentRow.ord)
    ).all()
    return [
        Segment(r.kind, r.text, r.text_hash, dict(r.locator), r.ord, r.lineage_id) for r in rows
    ]


def _event(session: Session, program_version_id: str) -> ChangeEvent | None:
    return session.scalar(
        sa.select(ChangeEvent).where(ChangeEvent.to_version_id == program_version_id)
    )


def record_change(
    session: Session,
    pv: ProgramVersion,
    store: BlobStore,
    *,
    new_id: Callable[[], str] | None = None,
) -> Recorded:
    """사업 버전 하나의 조각과 변경 이벤트를 남긴다. 이전 버전은 먼저 기록돼 있어야 한다.

    첨부가 다 끝나지 않은 버전은 기다린다(waiting). 읽지 못한 첨부의 옛 조각은 "알 수 없음"으로
    이벤트에 남기고, 다음 버전과 lineage가 이어지도록 이 버전의 조각에 표시를 달아 옮겨 둔다.
    """
    if (ev := _event(session, pv.id)) is not None:
        return Recorded(pv.version_no, "exists", ev)
    ready, waiting = attachments_settled(session, pv.notice_version_id)
    if not ready:
        return Recorded(pv.version_no, "waiting", waiting=waiting)

    prev = session.scalar(
        sa.select(ProgramVersion).where(
            ProgramVersion.program_id == pv.program_id,
            ProgramVersion.version_no == pv.version_no - 1,
        )
    )
    old: list[Segment] = []
    if prev is not None:
        if _event(session, prev.id) is None:
            raise ValueError(f"이전 버전 {prev.version_no}이 아직 기록되지 않았다")
        old = stored_segments(session, prev.notice_version_id)
    unreadable = unreadable_attachments(session, pv.notice_version_id)
    kept, unknown = comparable_old_segments(old, unreadable)

    nv = session.get(NoticeVersion, pv.notice_version_id)
    kwargs = {"new_id": new_id} if new_id else {}
    change = match_segments(kept, version_segments(session, nv, store), **kwargs)
    carried = [
        replace(s, locator={**s.locator, CARRIED: True}) for s in old if s.lineage_id in unknown
    ]
    for order, s in enumerate(change.segments + carried):
        session.add(
            SegmentRow(
                notice_version_id=nv.id,
                lineage_id=s.lineage_id,
                kind=s.kind,
                ord=order,
                text=s.text,
                text_hash=s.text_hash,
                locator=s.locator,
            )
        )
    ev = ChangeEvent(
        program_id=pv.program_id,
        from_version_id=prev.id if prev else None,
        to_version_id=pv.id,
        changed_lineages={
            "added": change.added,
            "removed": change.removed,
            "modified": change.modified,
            "unknown": sorted(unknown),
            "unchanged": len(change.unchanged),
        },
        matcher_version=MATCHER_VERSION,
    )
    session.add(ev)
    session.flush()
    return Recorded(pv.version_no, "recorded", ev)


def record_program(session: Session, program_id: str, store: BlobStore, **kwargs) -> list[Recorded]:
    """사업의 버전을 순서대로 기록한다. 기다리는 버전에서 멈춘다(뒤 버전은 그 버전과 비교해야 한다)."""
    out = []
    for pv in session.scalars(
        sa.select(ProgramVersion)
        .where(ProgramVersion.program_id == program_id)
        .order_by(ProgramVersion.version_no)
    ):
        r = record_change(session, pv, store, **kwargs)
        out.append(r)
        if r.status == "waiting":
            break
    return out


# --- 재생 (설계 10장) ---


@dataclass
class ReplayStep:
    event: ChangeEvent
    from_version: ProgramVersion | None
    to_version: ProgramVersion
    old_segments: list[Segment]  # 이전 버전 조각 (옮겨 둔 조각 포함)
    change: ChangeSet  # 새 버전 조각과 바뀐 lineage. 기록할 때와 같다.
    unknown: set[str]

    @property
    def total(self) -> int:
        return len(self.change.segments)

    def pairs(self) -> Iterator[tuple[Segment | None, Segment | None]]:
        """바뀐 조각을 (이전, 이후)로. 추가는 (None, 새 조각), 삭제는 (옛 조각, None)."""
        old = {s.lineage_id: s for s in self.old_segments}
        new = {s.lineage_id: s for s in self.change.segments}
        for lid in self.change.modified:
            yield old.get(lid), new[lid]
        for lid in self.change.added:
            yield None, new[lid]
        for lid in self.change.removed:
            yield old.get(lid), None


def replay(session: Session, program_id: str) -> Iterator[ReplayStep]:
    """기록된 이벤트를 버전 순서대로 다시 흘려보낸다. 매칭은 다시 돌리지 않는다."""
    versions = {
        pv.id: pv
        for pv in session.scalars(
            sa.select(ProgramVersion).where(ProgramVersion.program_id == program_id)
        )
    }
    events = sorted(
        session.scalars(sa.select(ChangeEvent).where(ChangeEvent.program_id == program_id)),
        key=lambda e: versions[e.to_version_id].version_no,
    )
    for ev in events:
        to = versions[ev.to_version_id]
        frm = versions.get(ev.from_version_id) if ev.from_version_id else None
        old = stored_segments(session, frm.notice_version_id) if frm else []
        new = [
            s for s in stored_segments(session, to.notice_version_id) if not s.locator.get(CARRIED)
        ]
        c = ev.changed_lineages
        modified, added = set(c["modified"]), set(c["added"])
        change = ChangeSet(
            segments=new,
            added=list(c["added"]),
            removed=list(c["removed"]),
            modified=list(c["modified"]),
            unchanged=[s.lineage_id for s in new if s.lineage_id not in modified | added],
        )
        yield ReplayStep(ev, frm, to, old, change, set(c["unknown"]))


def counter_ids(prefix: str = "L") -> Callable[[], str]:
    """테스트·재현용 결정적 lineage ID."""
    c = itertools.count(1)
    return lambda: f"{prefix}{next(c)}"
