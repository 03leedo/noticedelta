"""변경 영향 엔진 (설계 7.7).

바뀐 조각 → 영향 필드 → 부분 재추출 → 진짜 변경 판정 → 영향 사용자 재판정 → 알림.
LLM 호출(추출기)은 바뀐 조각 수와 영향 필드 수에만 비례하고 사용자 수와는 무관하다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from noticedelta.eligibility import Judgment, RuleGroup, judge
from noticedelta.matching import ChangeSet
from noticedelta.segments import Segment
from noticedelta.values import find_amounts, find_datetimes, values_equal

RULES_FIELD = "eligibility_rules"


@dataclass
class FieldValue:
    name: str
    value_type: str  # values.ValueType
    value: Any
    sources: list[str]  # 근거 조각 lineage_id
    confidence: float = 1.0


@dataclass
class Subscription:
    user_id: str
    fields: set[str]


@dataclass
class Alert:
    user_id: str
    notice_id: str
    field: str
    to_version_id: str
    kind: Literal["judgment_changed", "field_changed"]
    before: Any
    after: Any
    sources: list[str]
    idempotency_key: str


class Extractor(Protocol):
    def __call__(self, fields: set[str], segments: Sequence[Segment]) -> dict[str, FieldValue]: ...


@dataclass
class ImpactResult:
    affected_fields: set[str]
    reextracted: dict[str, FieldValue]
    real_changes: set[str]
    alerts: list[Alert]
    llm_fields_requested: int
    fields: dict[str, FieldValue] = field(default_factory=dict)  # 새 버전 기준 전체 필드


_KEYWORDS: dict[str, re.Pattern] = {
    "deadline": re.compile(r"마감|접수\s*기간|신청\s*기간|모집\s*기간|까지"),
    "amount": re.compile(r"지원\s*금|지원\s*규모|지원\s*한도|사업비|최대\s*\d"),
    RULES_FIELD: re.compile(
        r"신청\s*자격|지원\s*대상|자격\s*요건|제외\s*대상|업력|창업\s*\d+\s*년|만\s*\d+\s*세|"
        r"연령|나이|소재|예비\s*창업|재창업|여성|매출|상시\s*근로자|중복\s*수혜"
    ),
    "required_documents": re.compile(r"제출\s*서류|구비\s*서류|증빙|사본|서류"),
    "contact": re.compile(r"문의|연락처|담당자|☎|\d{2,3}-\d{3,4}-\d{4}"),
}


def keyword_candidates(seg: Segment) -> set[str]:
    """새로 추가·수정된 조각이 어느 필드와 관련 있을지 가볍게 고른다 (LLM 없이)."""
    out = {name for name, pat in _KEYWORDS.items() if pat.search(seg.text)}
    if find_datetimes(seg.text, default_year=2000):
        out.add("deadline")
    if find_amounts(seg.text):
        out.add("amount")
    return out


def build_field_source_index(fields: Mapping[str, FieldValue]) -> dict[str, set[str]]:
    """lineage_id → 그 조각을 근거로 쓰는 필드들 (설계 6장 field_source_index)."""
    index: dict[str, set[str]] = defaultdict(set)
    for name, fv in fields.items():
        for lid in fv.sources:
            index[lid].add(name)
    return dict(index)


def _key(user_id: str, notice_id: str, field_name: str, to_version_id: str) -> str:
    return f"{user_id}:{notice_id}:{field_name}:{to_version_id}"


def run_impact(
    *,
    notice_id: str,
    to_version_id: str,
    change: ChangeSet,
    old_fields: Mapping[str, FieldValue],
    extractor: Extractor,
    subscriptions: Sequence[Subscription],
    profiles: Mapping[str, Mapping[str, Any]],
    old_judgments: Mapping[str, Judgment],
    candidate_detector: Callable[[Segment], set[str]] = keyword_candidates,
    default_year: int | None = None,
) -> ImpactResult:
    # ① 영향 필드
    index = build_field_source_index(old_fields)
    affected: set[str] = set()
    for lid in change.changed:
        affected |= index.get(lid, set())
    touched = set(change.added) | set(change.modified)
    for seg in change.segments:
        if seg.lineage_id in touched:
            affected |= candidate_detector(seg)

    if not affected:
        return ImpactResult(set(), {}, set(), [], 0, dict(old_fields))

    # ② 부분 재추출: 영향 필드만, 바뀐 조각 + 그 필드의 기존 근거 조각만 넘긴다.
    context_ids = set(touched)
    for name in affected:
        if name in old_fields:
            context_ids |= set(old_fields[name].sources)
    context = [s for s in change.segments if s.lineage_id in context_ids]
    reextracted = extractor(affected, context)

    # ③ 진짜 변경 판정 (타입별 비교)
    real: set[str] = set()
    for name in affected:
        new = reextracted.get(name)
        if new is None:
            continue  # 다시 찾지 못한 필드는 기존 값을 유지한다.
        old = old_fields.get(name)
        if old is None or not values_equal(
            new.value_type, old.value, new.value, default_year=default_year
        ):
            real.add(name)

    fields = dict(old_fields)
    fields.update({n: reextracted[n] for n in real})

    # ④⑤⑥ 영향 사용자 재판정과 알림
    alerts: dict[str, Alert] = {}
    if RULES_FIELD in real:
        new_rules = RuleGroup.model_validate(fields[RULES_FIELD].value)
        for user_id, before in old_judgments.items():
            after = judge(new_rules, profiles.get(user_id, {}))
            if after.result != before.result:
                k = _key(user_id, notice_id, RULES_FIELD, to_version_id)
                alerts[k] = Alert(
                    user_id,
                    notice_id,
                    RULES_FIELD,
                    to_version_id,
                    "judgment_changed",
                    before.result,
                    after.result,
                    fields[RULES_FIELD].sources,
                    k,
                )
    for name in sorted(real - {RULES_FIELD}):
        for sub in subscriptions:
            if name in sub.fields:
                k = _key(sub.user_id, notice_id, name, to_version_id)
                old = old_fields.get(name)
                alerts[k] = Alert(
                    sub.user_id,
                    notice_id,
                    name,
                    to_version_id,
                    "field_changed",
                    old.value if old else None,
                    fields[name].value,
                    fields[name].sources,
                    k,
                )

    return ImpactResult(affected, reextracted, real, list(alerts.values()), len(affected), fields)
