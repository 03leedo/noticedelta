"""인용 검증: 추출한 값이 인용한 조각 텍스트에 실제로 있는지 코드로 확인한다 (설계 7.5)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from noticedelta.eligibility.rules import Attribute, Attributes, Rule, RuleGroup
from noticedelta.segments import normalize_text
from noticedelta.values import KST, find_amounts, find_datetimes, parse_krw, parse_kst_datetime

_INT = re.compile(r"\d[\d,]*")
_END_OF_DAY = (23, 59)


def _numbers(text: str) -> set[int]:
    return {int(m.replace(",", "")) for m in _INT.findall(text)}


def _same_moment(a: datetime, b: datetime) -> bool:
    if a == b:
        return True
    # 한쪽에만 시각이 있으면(다른 쪽은 그날 마감 23:59) 날짜만 비교한다.
    return a.date() == b.date() and _END_OF_DAY in ((a.hour, a.minute), (b.hour, b.minute))


def _verify_datetime(value: Any, texts: Sequence[str], default_year: int | None) -> bool:
    dt = (
        value
        if isinstance(value, datetime)
        else parse_kst_datetime(str(value), default_year=default_year)
    )
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return any(
        _same_moment(dt, found)
        for t in texts
        for found in find_datetimes(t, default_year=default_year)
    )


def _verify_amount(value: Any, texts: Sequence[str]) -> bool:
    amount = round(value) if isinstance(value, int | float) else parse_krw(str(value))
    return amount is not None and any(amount in find_amounts(t) for t in texts)


def _scaled(n: int, attr: Attribute | None) -> list[int]:
    """n과, 큰 단위로 적었을 때의 값(개월 84 → 년 7)."""
    if attr is not None and attr.scale and n % attr.scale == 0:
        return [n, n // attr.scale]
    return [n]


def _number_cited(n: int, attr: Attribute | None, joined: str) -> bool:
    if attr is not None and attr.money:
        return n in find_amounts(joined)
    nums = _numbers(joined)
    return any(c in nums for c in _scaled(n, attr))


_UPPER = ("이하", "이내", "미만", "까지")
_LOWER = ("이상", "초과", "부터")


def _unit_and_direction_ok(rule: Rule, n: int, joined: str, attr: Attribute | None) -> bool:
    """근거 문장에서 숫자 n 뒤의 단위와 "이하/이상" 방향이 규칙과 맞는지.

    단위는 속성 사전에서 온다. 단위가 다르면(마감 "31일"을 업력 31개월의 근거로) 근거로 보지 않는다.
    "만 39세 이하"를 근거로 나이 >= 39를 만들면 방향이 거꾸로다(평가 문서 I3).
    방향 말이 없으면 방향은 따지지 않는다.
    """
    if attr is None or not attr.units:
        return True
    unit = r"\s*(?:" + "|".join(re.escape(u) for u in attr.units) + ")"
    for c in _scaled(n, attr):
        for m in re.finditer(rf"(?<![\d,]){c}{unit}\s*(\S{{0,2}})", joined):
            word = m.group(1)
            if rule.op in ("<", "<=") and word.startswith(_LOWER):
                continue
            if rule.op in (">", ">=") and word.startswith(_UPPER):
                continue
            return True
    return False


def verify_rule(
    rule: Rule, cited_texts: Sequence[str], attributes: Attributes | None = None
) -> bool:
    """규칙 값이 근거 조각에 실제로 있는지. 속성 사전을 주면 단위·방향·금액 표기까지 본다."""
    if not cited_texts:
        return False  # 근거 조각이 없는 규칙은 검증되지 않은 것으로 본다.
    joined = normalize_text(" ".join(cited_texts))
    attr = (attributes or {}).get(rule.attribute)
    values = rule.value if isinstance(rule.value, list) else [rule.value]
    for v in values:
        if isinstance(v, bool):
            continue  # 예/아니오 조건은 숫자·문자 근거가 없다. 조각 인용만으로 둔다.
        if isinstance(v, int | float):
            if not _number_cited(round(v), attr, joined):
                return False
            if not _unit_and_direction_ok(rule, round(v), joined, attr):
                return False
        elif normalize_text(str(v)) not in joined:
            return False
    return True


def _iter_rules(group: RuleGroup):
    for node in group.rules:
        if isinstance(node, RuleGroup):
            yield from _iter_rules(node)
        else:
            yield node


def verify_citation(
    value_type: str,
    value: Any,
    cited_texts: Sequence[str],
    *,
    default_year: int | None = None,
    attributes: Attributes | None = None,
) -> bool:
    if value_type == "datetime":
        return _verify_datetime(value, cited_texts, default_year)
    if value_type == "amount":
        return _verify_amount(value, cited_texts)
    if value_type == "rules":
        group = value if isinstance(value, RuleGroup) else RuleGroup.model_validate(value)
        return all(verify_rule(r, cited_texts, attributes) for r in _iter_rules(group))
    joined = normalize_text(" ".join(cited_texts))
    if value_type == "list":
        return all(normalize_text(str(v)) in joined for v in value)
    return normalize_text(str(value)) in joined
