"""정답 라벨: 라벨할 정정 목록, 엔진과 무관한 원문 비교, 저장, 2차 표본, 일치도 (DESIGN 12.1).

라벨 화면은 엔진의 조각 비교를 보여 주지 않는다. API 응답 값과 본문을 글자 그대로 비교하고,
첨부는 원본 파일과 내용 해시만 보여 준다(파서와 무관).
"""

from __future__ import annotations

import difflib
import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import (
    Attachment,
    LabelRow,
    NoticeVersion,
    Program,
    ProgramVersion,
)
from noticedelta.llm.extract import FIELD_TYPES
from noticedelta.sources.base import _VOLATILE

FIELDS = tuple(FIELD_TYPES)
STATUSES = {
    "changed": "바뀜",
    "unchanged": "안 바뀜",
    "wording_only": "문구만 바뀜",
    "absent": "필드 없음",
}
VISIBLE = {"api_meta": "API 메타", "body": "본문", "attachment": "첨부"}
SECOND_ROUND_SHARE = 0.3
SECOND_ROUND_AFTER = timedelta(days=7)


@dataclass
class EventRef:
    program_id: str
    title: str
    from_version: ProgramVersion
    to_version: ProgramVersion
    rounds: set[int] = field(default_factory=set)  # 라벨을 단 회차


def events(session: Session, labeler: str = "me") -> list[EventRef]:
    """라벨할 정정: 버전이 둘 이상인 사업의 (이전, 이후) 쌍. 오래된 것부터."""
    out = []
    later = session.scalars(
        sa.select(ProgramVersion)
        .where(ProgramVersion.version_no >= 2)
        .order_by(ProgramVersion.created_at)
    ).all()
    for to in later:
        frm = session.scalar(
            sa.select(ProgramVersion).where(
                ProgramVersion.program_id == to.program_id,
                ProgramVersion.version_no == to.version_no - 1,
            )
        )
        if frm is None:
            continue
        rounds = set(
            session.scalars(
                sa.select(LabelRow.round)
                .where(LabelRow.to_version_id == to.id, LabelRow.labeler == labeler)
                .distinct()
            )
        )
        title = session.get(Program, to.program_id).title
        out.append(EventRef(to.program_id, title, frm, to, rounds))
    return out


# --- 원문 비교 ---


@dataclass
class EventView:
    ref: EventRef
    api_rows: list[tuple[str, str, str, bool]]  # (키, 이전, 이후, 바뀜)
    body_rows: list[tuple[str, str]]  # (표시: "=", "-", "+", "…"), 줄
    attachments: list[dict]


def _show(v) -> str:
    return "" if v is None else str(v)


def _api_rows(before: dict, after: dict) -> list[tuple[str, str, str, bool]]:
    keys = sorted((set(before) | set(after)) - _VOLATILE)
    rows = [(k, _show(before.get(k)), _show(after.get(k))) for k in keys]
    rows = [(k, b, a, b != a) for k, b, a in rows]
    return sorted(rows, key=lambda r: not r[3])  # 바뀐 키를 위로


def _body_rows(before: str, after: str, context: int = 2) -> list[tuple[str, str]]:
    a, b = before.splitlines(), after.splitlines()
    out = []
    for group in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_grouped_opcodes(context):
        if out:
            out.append(("…", ""))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += [("=", line) for line in a[i1:i2]]
            if tag in ("delete", "replace"):
                out += [("-", line) for line in a[i1:i2]]
            if tag in ("insert", "replace"):
                out += [("+", line) for line in b[j1:j2]]
    return out


def _attachments(session: Session, frm: NoticeVersion, to: NoticeVersion) -> list[dict]:
    def rows(nv: NoticeVersion) -> dict[str, Attachment]:
        atts = session.scalars(
            sa.select(Attachment).where(Attachment.notice_version_id == nv.id)
        ).all()
        return {a.lineage_key or a.filename: a for a in atts}

    old, new = rows(frm), rows(to)
    out = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        if o is None:
            state = "새로 생김"
        elif n is None:
            state = "없어짐"
        elif o.blob_key is None or n.blob_key is None:
            state = "받지 못함"
        else:
            state = "같음" if o.blob_key == n.blob_key else "바뀜"
        out.append({"key": key, "old": o, "new": n, "state": state})
    return out


def event_view(session: Session, to_version_id: str, labeler: str = "me") -> EventView | None:
    ref = next((e for e in events(session, labeler) if e.to_version.id == to_version_id), None)
    if ref is None:
        return None
    frm = session.get(NoticeVersion, ref.from_version.notice_version_id)
    to = session.get(NoticeVersion, ref.to_version.notice_version_id)
    return EventView(
        ref,
        _api_rows(frm.raw or {}, to.raw or {}),
        _body_rows(frm.body or "", to.body or ""),
        _attachments(session, frm, to),
    )


# --- 저장 ---


def save_labels(
    session: Session,
    ref: EventRef,
    *,
    round: int,
    labeler: str,
    rows: dict[str, dict],
    now: datetime,
) -> int:
    """필드마다 라벨을 쓴다. 같은 회차를 다시 저장하면 덮어쓴다. 쓴 필드 수를 준다."""
    n = 0
    for name in FIELDS:
        r = rows.get(name) or {}
        if r.get("status") not in STATUSES:
            continue
        row = session.scalar(
            sa.select(LabelRow).where(
                LabelRow.to_version_id == ref.to_version.id,
                LabelRow.field == name,
                LabelRow.round == round,
                LabelRow.labeler == labeler,
            )
        )
        if row is None:
            row = LabelRow(
                program_id=ref.program_id,
                to_version_id=ref.to_version.id,
                field=name,
                round=round,
                labeler=labeler,
            )
            session.add(row)
        row.status = r["status"]
        row.before = (r.get("before") or "").strip() or None
        row.after = (r.get("after") or "").strip() or None
        row.visible_in = r.get("visible_in") if r.get("visible_in") in VISIBLE else None
        row.evidence = (r.get("evidence") or "").strip() or None
        row.note = (r.get("note") or "").strip() or None
        row.labeled_at = now
        n += 1
    session.flush()
    return n


# --- 2차 표본과 일치도 ---


def second_round(
    session: Session, now: datetime, *, labeler: str = "me", seed: str = "noticedelta"
) -> list[EventRef]:
    """1차 라벨을 단 지 7일이 지난 정정 중 30%(올림). 순서는 시드로 고정해 다시 뽑아도 같다."""
    cutoff = now - SECOND_ROUND_AFTER
    done = {
        v: t
        for v, t in session.execute(
            sa.select(LabelRow.to_version_id, sa.func.max(LabelRow.labeled_at))
            .where(LabelRow.round == 1, LabelRow.labeler == labeler)
            .group_by(LabelRow.to_version_id)
        )
    }
    pool = []
    for e in events(session, labeler):
        t = done.get(e.to_version.id)
        if t is None:
            continue
        if t.tzinfo is None:  # SQLite는 시간대를 지운다
            t = t.replace(tzinfo=now.tzinfo)
        if t <= cutoff:
            pool.append(e)
    pool.sort(key=lambda e: hashlib.sha256(f"{seed}:{e.to_version.id}".encode()).hexdigest())
    return pool[: math.ceil(len(pool) * SECOND_ROUND_SHARE)]


@dataclass
class Agreement:
    pairs: int
    agree: float | None  # 상태가 같은 비율
    kappa: float | None  # Cohen's kappa


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def agreement(session: Session, labeler: str = "me") -> Agreement:
    """같은 사람의 1차와 2차 상태 라벨이 얼마나 같은가 (필드 단위)."""
    rows = session.scalars(sa.select(LabelRow).where(LabelRow.labeler == labeler)).all()
    by = {(r.to_version_id, r.field, r.round): r.status for r in rows}
    pairs = [(by[(v, f, 1)], s) for (v, f, rnd), s in by.items() if rnd == 2 and (v, f, 1) in by]
    if not pairs:
        return Agreement(0, None, None)
    first, second = [p[0] for p in pairs], [p[1] for p in pairs]
    agree = sum(x == y for x, y in pairs) / len(pairs)
    return Agreement(len(pairs), agree, cohen_kappa(first, second))
