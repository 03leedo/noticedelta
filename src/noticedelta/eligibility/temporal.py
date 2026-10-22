"""시간에 따라 바뀌는 자격과 "거의 해당" (DESIGN.md 14장 5주차, ARCHITECTURE.md 6.1).

자격 규칙이 코드라서 "언제 자격이 바뀌는지"를 계산할 수 있다.

- 프로필에 개업일(`founded_on`)·대표자 생년월일(`ceo_birth_date`)이 있으면 날짜마다
  `business_age_months`(만 개월)·`ceo_age`(만 나이)를 다시 계산한다.
- 규칙의 경계값에서 속성이 바뀌는 날만 후보로 뽑아 판정을 다시 돌린다. 날마다 돌지 않는다.
- 공고가 "공고일 기준"처럼 기준일을 정해 두면 그날 하루만 판정한다(`fixed_on`).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from noticedelta.eligibility.judge import Judgment, Reason, Result, _coerce, _eval_rule, judge
from noticedelta.eligibility.rules import Rule, RuleGroup

FOUNDED_ON = "founded_on"
CEO_BIRTH_DATE = "ceo_birth_date"


def _as_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    digits = re.sub(r"\D", "", str(v))  # 2019-11-01, 2019.11.01, 2019/11/01, 20191101
    if len(digits) < 8:
        raise ValueError(f"날짜를 읽을 수 없음: {v!r}")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def months_between(start: date, on: date) -> int:
    """만 개월 수. 1월 31일 개업이면 2월 말일에 1개월이 찬다."""
    m = (on.year - start.year) * 12 + (on.month - start.month)
    if on.day < start.day and not _is_month_end(on):
        m -= 1
    return max(m, 0)


def add_months(start: date, n: int) -> date:
    y, m = divmod(start.month - 1 + n, 12)
    year, month = start.year + y, m + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def age_on(birth: date, on: date) -> int:
    """만 나이. 2월 29일생은 평년에 3월 1일에 나이가 오른다."""
    years = on.year - birth.year
    if (on.month, on.day) < (birth.month, birth.day):
        years -= 1
    return max(years, 0)


def add_years(birth: date, n: int) -> date:
    try:
        return birth.replace(year=birth.year + n)
    except ValueError:  # 2월 29일
        return date(birth.year + n, 3, 1)


def derive_profile(profile: Mapping[str, Any], on: date) -> dict[str, Any]:
    """기준일의 업력·나이를 채운 프로필. 날짜가 없으면 원래 값을 그대로 둔다."""
    out = dict(profile)
    if (founded := _as_date(profile.get(FOUNDED_ON))) is not None:
        out["business_age_months"] = months_between(founded, on) if on >= founded else 0
    if (birth := _as_date(profile.get(CEO_BIRTH_DATE))) is not None:
        out["ceo_age"] = age_on(birth, on)
    return out


def _walk(group: RuleGroup) -> Iterator[Rule]:
    for node in group.rules:
        if isinstance(node, RuleGroup):
            yield from _walk(node)
        else:
            yield node


def _thresholds(rule: Rule) -> list[int]:
    vals = rule.value if isinstance(rule.value, list) else [rule.value]
    return [int(v) for v in vals if isinstance(v, int | float) and not isinstance(v, bool)]


def _candidate_dates(group: RuleGroup, profile: Mapping[str, Any]) -> set[date]:
    """속성 값이 규칙 경계를 넘나드는 날. 경계값 v와 v+1에 닿는 날을 모두 넣는다."""
    founded = _as_date(profile.get(FOUNDED_ON))
    birth = _as_date(profile.get(CEO_BIRTH_DATE))
    out: set[date] = set()
    for rule in _walk(group):
        for v in _thresholds(rule):
            for n in (v, v + 1):
                if rule.attribute == "business_age_months" and founded is not None:
                    out.add(add_months(founded, n))
                elif rule.attribute == "ceo_age" and birth is not None:
                    out.add(add_years(birth, n))
    return out


@dataclass
class Change:
    on: date
    before: Result
    after: Result
    reasons: list[Reason]  # 결과가 바뀐 규칙들의 새 판정 근거


@dataclass
class Timeline:
    as_of: date
    current: Judgment
    changes: list[Change] = field(default_factory=list)

    @property
    def next_change(self) -> Change | None:
        return self.changes[0] if self.changes else None


def eligibility_timeline(
    group: RuleGroup,
    profile: Mapping[str, Any],
    *,
    start: date,
    until: date | None = None,
    horizon_days: int = 365,
    fixed_on: date | None = None,
    min_confidence: float = 0.7,
) -> Timeline:
    """start부터 until(보통 공고 마감일, 없으면 horizon_days 뒤)까지 판정이 바뀌는 날.

    fixed_on이 있으면(예: "공고일 기준 창업 7년 이내") 그날 기준으로 한 번만 판정한다.
    """
    if fixed_on is not None:
        return Timeline(
            fixed_on, judge(group, derive_profile(profile, fixed_on), min_confidence=min_confidence)
        )
    end = until if until is not None else start + timedelta(days=horizon_days)
    prev = judge(group, derive_profile(profile, start), min_confidence=min_confidence)
    timeline = Timeline(start, prev)
    for d in sorted(x for x in _candidate_dates(group, profile) if start < x <= end):
        cur = judge(group, derive_profile(profile, d), min_confidence=min_confidence)
        if cur.result != prev.result:
            changed = [
                new
                for old, new in zip(prev.reasons, cur.reasons, strict=True)
                if old.outcome != new.outcome
            ]
            timeline.changes.append(Change(d, prev.result, cur.result, changed))
        prev = cur
    return timeline


@dataclass
class NearMiss:
    rule: Rule
    reason: Reason
    result_if_met: Result  # 이 조건만 맞으면 나오는 결과 (해당 또는 확인 필요)
    gap: float | None  # 숫자 조건이면 기준과의 차이 (예: 매출이 기준보다 2천만 원 많음 → 20000000)


def _gap(rule: Rule, actual: Any) -> float | None:
    if not isinstance(actual, int | float) or isinstance(actual, bool):
        return None
    v = rule.value
    if rule.op in ("<", "<="):
        return actual - v
    if rule.op in (">", ">="):
        return v - actual
    if rule.op == "between":
        lo, hi = v
        return lo - actual if actual < lo else actual - hi
    return None


def near_misses(
    group: RuleGroup,
    profile: Mapping[str, Any],
    *,
    on: date | None = None,
    min_confidence: float = 0.7,
) -> list[NearMiss]:
    """비해당인 공고에서, 조건 하나만 맞으면 비해당을 벗어나는 규칙들.

    on을 주면 그날 기준 업력·나이를 채운 프로필로 본다. 개업일만 있는 프로필에서 업력 조건이
    "값 없음"으로 빠져 다른 조건 하나만 다른 것처럼 보이지 않게 한다.
    """
    if on is not None:
        profile = derive_profile(profile, on)
    base = judge(group, profile, min_confidence=min_confidence)
    if base.result != "ineligible":
        return []
    out = []
    for rule in _walk(group):
        tri, reason = _eval_rule(rule, profile, min_confidence, frozenset())
        if tri != "fail":
            continue
        alt = judge(group, profile, min_confidence=min_confidence, assume_pass=[rule])
        if alt.result != "ineligible":
            actual = _coerce(profile.get(rule.attribute), rule.value)
            out.append(NearMiss(rule, reason, alt.result, _gap(rule, actual)))
    return out
