"""대량 판정용 SQL 조건 (설계 7.6). '확정 비해당'이 아닌 프로필만 남긴다."""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.sql.elements import ColumnElement

from noticedelta.eligibility.rules import Rule, RuleGroup


def _rule_condition(rule: Rule, col: ColumnElement) -> ColumnElement:
    v = rule.value
    match rule.op:
        case "==":
            return col == v
        case "!=":
            return col != v
        case "<":
            return col < v
        case "<=":
            return col <= v
        case ">":
            return col > v
        case ">=":
            return col >= v
        case "in":
            return col.in_(v)
        case "not_in":
            return col.not_in(v)
        case "between":
            return col.between(v[0], v[1])
    raise ValueError(rule.op)


def _fails(
    node: Rule | RuleGroup, column: Callable[[str], ColumnElement], min_confidence: float
) -> ColumnElement:
    """이 노드가 확실히 거짓인 조건. 값이 NULL이거나 신뢰도가 낮으면 거짓으로 보지 않는다."""
    if isinstance(node, Rule):
        if node.confidence < min_confidence:
            return sa.false()
        col = column(node.attribute)
        return sa.and_(col.is_not(None), sa.not_(_rule_condition(node, col)))
    children = [_fails(n, column, min_confidence) for n in node.rules]
    if not children:
        return sa.false()
    return sa.or_(*children) if node.group == "all_of" else sa.and_(*children)


def to_sql_where(
    group: RuleGroup, column: Callable[[str], ColumnElement], *, min_confidence: float = 0.7
) -> ColumnElement:
    """해당 또는 확인 필요 후보만 남기는 WHERE 조건. 최종 판정은 judge()로 한다."""
    return sa.not_(_fails(group, column, min_confidence))
