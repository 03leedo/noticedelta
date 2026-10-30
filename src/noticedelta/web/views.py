"""판정 화면이 보여 줄 것을 계산한다 (작업 목록 7번). 웹 프레임워크와 무관한 순수 계산 + DB 읽기.

한 사람의 프로필로 열린 사업마다 판정하고, 해당 / 거의 해당 / 확인 필요로 나눈다. 판정마다 원문
근거 문장과, 시간이 지나 판정이 바뀌는 날(자격 상실·획득 예정일)을 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import FieldValueRow, Program, ProgramVersion, SegmentRow
from noticedelta.eligibility import RuleGroup, judge
from noticedelta.eligibility.judge import Judgment
from noticedelta.eligibility.temporal import (
    Change,
    NearMiss,
    derive_profile,
    eligibility_timeline,
    near_misses,
)
from noticedelta.impact import RULES_FIELD

GROUPS = ("eligible", "near", "unknown")
GROUP_NAMES = {"eligible": "해당", "near": "거의 해당", "unknown": "확인 필요"}
OUTCOME_NAMES = {
    "pass": "충족",
    "fail": "불충족",
    "missing": "값 없음",
    "low_confidence": "규칙 확인 필요",
    "manual": "직접 확인",
}


@dataclass
class ProgramView:
    program_id: str
    title: str
    org: str | None
    deadline: date | None
    judgment: Judgment
    sources: dict[str, str]  # 근거 lineage → 원문 문장
    next_change: Change | None = None
    near: list[NearMiss] = field(default_factory=list)

    @property
    def group(self) -> str | None:
        if self.judgment.result == "eligible":
            return "eligible"
        if self.judgment.result == "unknown":
            return "unknown"
        return "near" if self.near else None


@dataclass
class MyPrograms:
    as_of: date
    items: list[ProgramView]
    hidden: int  # 비해당이라 보여 주지 않은 사업 수

    def group(self, name: str) -> list[ProgramView]:
        return [p for p in self.items if p.group == name]

    def counts(self) -> dict[str, int]:
        return {g: len(self.group(g)) for g in GROUPS}


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _current_fields(session: Session) -> dict[str, tuple[ProgramVersion, dict[str, FieldValueRow]]]:
    """사업마다 자격 규칙이 있는 가장 최근 버전과 그 버전의 필드들."""
    latest = (
        sa.select(ProgramVersion.program_id, sa.func.max(ProgramVersion.version_no).label("no"))
        .join(FieldValueRow, FieldValueRow.program_version_id == ProgramVersion.id)
        .where(FieldValueRow.name == RULES_FIELD)
        .group_by(ProgramVersion.program_id)
        .subquery()
    )
    pvs = session.scalars(
        sa.select(ProgramVersion).join(
            latest,
            (ProgramVersion.program_id == latest.c.program_id)
            & (ProgramVersion.version_no == latest.c.no),
        )
    ).all()
    out = {}
    for pv in pvs:
        rows = session.scalars(
            sa.select(FieldValueRow).where(FieldValueRow.program_version_id == pv.id)
        ).all()
        out[pv.program_id] = (pv, {r.name: r for r in rows})
    return out


def _source_texts(session: Session, pv: ProgramVersion, lids: set[str]) -> dict[str, str]:
    if not lids:
        return {}
    rows = session.execute(
        sa.select(SegmentRow.lineage_id, SegmentRow.text).where(
            SegmentRow.notice_version_id == pv.notice_version_id,
            SegmentRow.lineage_id.in_(lids),
        )
    ).all()
    return dict(rows)


def my_programs(session: Session, profile: dict[str, Any], today: date) -> MyPrograms:
    items, hidden = [], 0
    for program_id, (pv, fields) in _current_fields(session).items():
        deadline = _as_date(fields["deadline"].value) if "deadline" in fields else None
        if deadline is not None and deadline < today:
            continue  # 마감이 지난 사업은 보이지 않는다
        rules = RuleGroup.model_validate(fields[RULES_FIELD].value)
        current = judge(rules, derive_profile(profile, today))
        timeline = eligibility_timeline(rules, profile, start=today, until=deadline)
        lids = set(fields[RULES_FIELD].source_lineage_ids) | {
            r.source for r in current.reasons if r.source
        }
        program = session.get(Program, program_id)
        view = ProgramView(
            program_id=program_id,
            title=program.title,
            org=program.org,
            deadline=deadline,
            judgment=current,
            sources=_source_texts(session, pv, lids),
            next_change=timeline.next_change,
            near=near_misses(rules, profile, on=today),
        )
        if view.group is None:
            hidden += 1
        else:
            items.append(view)
    # 마감이 가까운 것부터, 마감이 없으면 뒤로
    items.sort(key=lambda v: (v.deadline is None, v.deadline or date.max, v.title))
    return MyPrograms(today, items, hidden)


ATTRIBUTE_NAMES = {
    "business_age_months": "업력(개월)",
    "ceo_age": "대표자 나이",
    "region_sido": "소재지",
    "industry_ksic": "업종 코드",
    "business_type": "사업자 형태",
    "revenue_krw": "매출(원)",
    "employees": "상시 근로자 수",
    "is_pre_founder": "예비 창업자",
    "is_women_owned": "여성기업",
    "is_restart": "재창업",
}
_OPS = {
    " <= ": " ≤ ",
    " >= ": " ≥ ",
    " == ": " = ",
    " != ": " ≠ ",
    " not_in ": " 제외: ",
    " in ": " 중 하나: ",
    " between ": " 범위 ",
}
_VALUES = {"True": "예", "False": "아니오", "'individual'": "개인", "'corporation'": "법인"}


def humanize(detail: str) -> str:
    """판정 근거 문장("business_age_months <= 84 (프로필: 72)")을 읽기 쉬운 말로."""
    for key, name in ATTRIBUTE_NAMES.items():
        detail = detail.replace(key, name)
    for op, word in {**_OPS, **_VALUES}.items():
        detail = detail.replace(op, word)
    return detail.replace("프로필 값 없음", "입력하지 않음")
