"""결정적 자격 판정기: 해당(eligible) / 비해당(ineligible) / 확인 필요(unknown)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from noticedelta.eligibility.rules import Rule, RuleGroup

Outcome = Literal["pass", "fail", "missing", "low_confidence", "manual"]
Result = Literal["eligible", "ineligible", "unknown"]
_Tri = Literal["pass", "fail", "unknown"]


@dataclass
class Reason:
    rule_id: str | None
    outcome: Outcome
    detail: str
    source: str | None = None


@dataclass
class Judgment:
    result: Result
    reasons: list[Reason] = field(default_factory=list)


def _compare(op: str, actual: Any, expected: Any) -> bool:
    match op:
        case "==":
            return actual == expected
        case "!=":
            return actual != expected
        case "<":
            return actual < expected
        case "<=":
            return actual <= expected
        case ">":
            return actual > expected
        case ">=":
            return actual >= expected
        case "in":
            return actual in expected
        case "not_in":
            return actual not in expected
        case "between":
            lo, hi = expected
            return lo <= actual <= hi
    raise ValueError(op)


TRUE_WORDS = ("true", "t", "y", "yes", "예", "네", "1")
FALSE_WORDS = ("false", "f", "n", "no", "아니오", "아니요", "0")
_TRUE, _FALSE = set(TRUE_WORDS), set(FALSE_WORDS)
# 폼 문자열을 숫자로 읽는 형식. 대량 판정 SQL(sql.py)도 같은 형식만 숫자로 읽는다.
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_UNCOERCIBLE = object()


def _coerce(actual: Any, expected: Any) -> Any:
    """폼에서 온 문자열을 규칙 값의 타입에 맞춘다. 맞출 수 없으면 _UNCOERCIBLE."""
    if isinstance(actual, list | dict):
        return _UNCOERCIBLE  # 목록·객체 값은 어떤 속성으로도 읽지 않는다
    sample = expected[0] if isinstance(expected, list | tuple) and expected else expected
    if isinstance(sample, bool):
        if isinstance(actual, bool):
            return actual
        s = str(actual).strip().lower()
        return True if s in _TRUE else False if s in _FALSE else _UNCOERCIBLE
    if isinstance(sample, int | float):
        if isinstance(actual, bool):
            return _UNCOERCIBLE  # 참/거짓을 1/0 숫자로 보지 않는다
        if isinstance(actual, str):
            s = actual.replace(",", "").strip()
            if not NUMBER.fullmatch(s):
                return actual  # 비교에서 TypeError가 나 확인 필요로 간다
            return int(s) if s.lstrip("+-").isdigit() else float(s)
    return actual


def _eval_rule(
    rule: Rule, profile: Mapping[str, Any], min_confidence: float, assumed: frozenset[int]
) -> tuple[_Tri, Reason]:
    desc = f"{rule.attribute} {rule.op} {rule.value!r}"
    if id(rule) in assumed:
        return "pass", Reason(rule.id, "pass", f"{desc} (충족한다고 가정)", rule.source)
    if rule.confidence < min_confidence:
        return "unknown", Reason(
            rule.id, "low_confidence", f"{desc} (규칙 신뢰도 {rule.confidence})", rule.source
        )
    actual = profile.get(rule.attribute)
    if actual is None:
        return "unknown", Reason(rule.id, "missing", f"{desc} (프로필 값 없음)", rule.source)
    actual = _coerce(actual, rule.value)
    try:
        if actual is _UNCOERCIBLE:
            raise TypeError
        ok = _compare(rule.op, actual, rule.value)
    except TypeError:
        return "unknown", Reason(
            rule.id, "missing", f"{desc} (프로필 값 형식 오류: {actual!r})", rule.source
        )
    return ("pass" if ok else "fail"), Reason(
        rule.id, "pass" if ok else "fail", f"{desc} (프로필: {actual!r})", rule.source
    )


def _eval_group(
    group: RuleGroup, profile: Mapping[str, Any], min_confidence: float, assumed: frozenset[int]
) -> tuple[_Tri, list[Reason]]:
    outcomes: list[_Tri] = []
    reasons: list[Reason] = []
    for node in group.rules:
        if isinstance(node, RuleGroup):
            o, rs = _eval_group(node, profile, min_confidence, assumed)
        else:
            o, r = _eval_rule(node, profile, min_confidence, assumed)
            rs = [r]
        outcomes.append(o)
        reasons.extend(rs)

    if group.group == "all_of":
        result: _Tri = (
            "fail" if "fail" in outcomes else "unknown" if "unknown" in outcomes else "pass"
        )
    else:
        result = "pass" if "pass" in outcomes else "unknown" if "unknown" in outcomes else "fail"
    if not outcomes and not group.manual:
        # 규칙이 하나도 없는 그룹을 "누구나 해당"으로 보지 않는다. 추출이 실패했거나(자격 조건이
        # 이미지·깨진 표에 있음) 숨은 글로 조작됐을 수 있다 (평가 문서 I3).
        result = "unknown"
        reasons.append(Reason(None, "missing", "자격 규칙이 하나도 없음 (공고 원문 확인 필요)"))

    for m in group.manual:
        reasons.append(Reason(None, "manual", f"직접 확인 필요: {m.text}", m.source))
    if group.manual and result == "pass":
        result = "unknown"
    return result, reasons


def judge(
    group: RuleGroup,
    profile: Mapping[str, Any],
    *,
    min_confidence: float = 0.7,
    assume_pass: Iterable[Rule] = (),
) -> Judgment:
    """assume_pass의 규칙은 충족한다고 보고 판정한다("이 조건만 맞으면?" 계산용)."""
    assumed = frozenset(id(r) for r in assume_pass)
    tri, reasons = _eval_group(group, profile, min_confidence, assumed)
    result: Result = {"pass": "eligible", "fail": "ineligible", "unknown": "unknown"}[tri]
    return Judgment(result, reasons)
