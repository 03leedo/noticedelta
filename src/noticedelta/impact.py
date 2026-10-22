"""변경 영향 엔진 (설계 7.7).

바뀐 조각 → 영향 필드 → 부분 재추출 → 진짜 변경 판정 → 영향 사용자 재판정 → 알림.
LLM 호출(추출기)은 바뀐 조각 수와 영향 필드 수에만 비례하고 사용자 수와는 무관하다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from noticedelta.eligibility import Judgment, RuleGroup, judge
from noticedelta.matching import ChangeSet
from noticedelta.segments import Segment
from noticedelta.values import find_amounts, find_datetimes, values_equal

RULES_FIELD = "eligibility_rules"
REVIEW_THRESHOLD = 0.7


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
    confidence: float = 1.0

    @property
    def needs_review(self) -> bool:
        """인용 검증을 통과하지 못한 값이면 "확인 필요"로 보여 준다 (설계 7.5)."""
        return self.confidence < REVIEW_THRESHOLD


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
    stale: set[str] = field(default_factory=set)  # 근거가 바뀌었는데 다시 찾지 못한 필드


STALE_CONFIDENCE = 0.3  # 판정기의 기본 신뢰도 하한(0.7)보다 낮아 확인 필요로 간다
_NOT_ELIGIBLE = Judgment("ineligible")  # 판정 기록이 없는 사용자의 이전 상태


_KEYWORDS: dict[str, re.Pattern] = {
    # 날짜가 있는 조각은 find_datetimes가 따로 잡는다. "까지"만으로는 고르지 않는다.
    "deadline": re.compile(r"마감|접수\s*기간|신청\s*기간|모집\s*기간|소진\s*시\s*까지"),
    "amount": re.compile(r"지원\s*금|지원\s*규모|지원\s*한도|사업비|최대\s*\d"),
    RULES_FIELD: re.compile(
        r"신청\s*자격|지원\s*대상|자격\s*요건|제외\s*대상|업력|창업\s*\d+\s*년|만\s*\d+\s*세|"
        r"연령|나이|소재|예비\s*창업|재창업|여성|매출|상시\s*근로자|중복\s*수혜"
    ),
    # "서류 평가"처럼 서류만 들어간 문장은 고르지 않는다.
    "required_documents": re.compile(
        r"(?:제출|구비|증빙|첨부|신청|필요|준비)\s*서류|서류\s*(?:제출|목록)|증빙\s*자료|사본"
    ),
    "contact": re.compile(r"문의|연락처|담당자|☎|\d{2,3}-\d{3,4}-\d{4}"),
}


def keyword_candidates(seg: Segment) -> set[str]:
    """새로 추가·수정·삭제된 조각이 어느 필드와 관련 있을지 가볍게 고른다 (LLM 없이).

    표 칸은 칸 글만으로는 모른다("5년"). 열 머리글과 행 키("업력")까지 함께 본다.
    """
    text = seg.text
    if seg.kind == "table_cell":
        loc = seg.locator
        text = f"{loc.get('header', '')} {loc.get('row_key', '')} {seg.text}"
    out = {name for name, pat in _KEYWORDS.items() if pat.search(text)}
    if find_datetimes(text, default_year=2000):
        out.add("deadline")
    if find_amounts(text):
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
    old_segments: Sequence[Segment] = (),
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
    # 인용되지 않은 조항이 지워진 경우("단, 재창업자는 제외")도 옛 글로 검사해 잡는다.
    removed = set(change.removed)
    for seg in old_segments:
        if seg.lineage_id in removed:
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
    stale: set[str] = set()
    changed_ids = set(change.changed)
    for name in affected:
        new = reextracted.get(name)
        if new is None:
            # 근거 조각이 바뀌거나 지워졌는데 다시 찾지 못했다. 옛 값을 조용히 믿지 않고 낮춘다.
            old = old_fields.get(name)
            if old is not None and set(old.sources) & changed_ids:
                stale.add(name)
            continue
        old = old_fields.get(name)
        if old is None or not values_equal(
            new.value_type, old.value, new.value, default_year=default_year
        ):
            real.add(name)

    fields = dict(old_fields)
    for name in stale:
        fields[name] = replace(
            fields[name], confidence=min(fields[name].confidence, STALE_CONFIDENCE)
        )
    for name, new in reextracted.items():
        if name in real:
            fields[name] = new
        elif name in fields:
            # 값은 같아도 근거 조각은 새 버전 기준으로 바꾼다. 지워진 조각 ID가 남으면 다음 정정을 놓친다.
            fields[name] = replace(fields[name], sources=new.sources, confidence=new.confidence)

    # ④⑤⑥ 영향 사용자 재판정과 알림
    alerts: dict[str, Alert] = {}
    if RULES_FIELD in real:
        new_rules = RuleGroup.model_validate(fields[RULES_FIELD].value)
        # 판정 기록이 없는 사용자도 본다. 조건이 완화돼 새로 해당하게 된 사람에게도 알려야 한다.
        for user_id in sorted(set(old_judgments) | set(profiles)):
            before = old_judgments.get(user_id, _NOT_ELIGIBLE)
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
                    fields[RULES_FIELD].confidence,
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
                    fields[name].confidence,
                )

    return ImpactResult(
        affected, reextracted, real, list(alerts.values()), len(affected), fields, stale
    )
