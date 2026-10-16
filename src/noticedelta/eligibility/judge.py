"""결정적 자격 판정기: 해당(eligible) / 비해당(ineligible) / 확인 필요(unknown)."""

from __future__ import annotations

from collections.abc import Mapping
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


def _eval_rule(
    rule: Rule, profile: Mapping[str, Any], min_confidence: float
) -> tuple[_Tri, Reason]:
    desc = f"{rule.attribute} {rule.op} {rule.value!r}"
    if rule.confidence < min_confidence:
        return "unknown", Reason(
            rule.id, "low_confidence", f"{desc} (규칙 신뢰도 {rule.confidence})", rule.source
        )
    actual = profile.get(rule.attribute)
    if actual is None:
        return "unknown", Reason(rule.id, "missing", f"{desc} (프로필 값 없음)", rule.source)
    try:
        ok = _compare(rule.op, actual, rule.value)
    except TypeError:
        return "unknown", Reason(
            rule.id, "missing", f"{desc} (프로필 값 형식 오류: {actual!r})", rule.source
        )
    return ("pass" if ok else "fail"), Reason(
        rule.id, "pass" if ok else "fail", f"{desc} (프로필: {actual!r})", rule.source
    )


def _eval_group(
    group: RuleGroup, profile: Mapping[str, Any], min_confidence: float
) -> tuple[_Tri, list[Reason]]:
    outcomes: list[_Tri] = []
    reasons: list[Reason] = []
    for node in group.rules:
        if isinstance(node, RuleGroup):
            o, rs = _eval_group(node, profile, min_confidence)
        else:
            o, r = _eval_rule(node, profile, min_confidence)
            rs = [r]
        outcomes.append(o)
        reasons.extend(rs)

    if group.group == "all_of":
        result: _Tri = (
            "fail" if "fail" in outcomes else "unknown" if "unknown" in outcomes else "pass"
        )
    else:
        result = "pass" if "pass" in outcomes else "unknown" if "unknown" in outcomes else "fail"
        if not outcomes:
            result = "pass"

    for m in group.manual:
        reasons.append(Reason(None, "manual", f"직접 확인 필요: {m.text}", m.source))
    if group.manual and result == "pass":
        result = "unknown"
    return result, reasons


def judge(group: RuleGroup, profile: Mapping[str, Any], *, min_confidence: float = 0.7) -> Judgment:
    tri, reasons = _eval_group(group, profile, min_confidence)
    result: Result = {"pass": "eligible", "fail": "ineligible", "unknown": "unknown"}[tri]
    return Judgment(result, reasons)
