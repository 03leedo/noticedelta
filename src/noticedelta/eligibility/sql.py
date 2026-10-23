"""대량 판정용 SQL (설계 7.6).

- to_sql_where: '확정 비해당'이 아닌 프로필만 남기는 WHERE 조건.
- tri_state / reasons_json / bulk_judge: judge()와 같은 3상태 판정과 규칙별 근거를 SQL 한 문장으로
  계산해 judgment 테이블에 쌓는다. 사용자 수만큼 파이썬을 돌지 않고 DB가 한 번에 판정한다.

3상태는 정수로 둔다: 해당 1, 확인 필요 0, 비해당 -1. all_of는 자식의 최솟값, any_of는 최댓값이라
judge()의 규칙(하나라도 비해당이면 비해당, 아니면 하나라도 모르면 확인 필요)과 같다.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import GenericFunction

from noticedelta.eligibility.judge import FALSE_WORDS, NUMBER, TRUE_WORDS
from noticedelta.eligibility.rules import ATTRIBUTES, Rule, RuleGroup, ruleset_hash

Column = Callable[[str], ColumnElement]


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


def _fails(node: Rule | RuleGroup, column: Column, min_confidence: float) -> ColumnElement:
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


def to_sql_where(group: RuleGroup, column: Column, *, min_confidence: float = 0.7) -> ColumnElement:
    """해당 또는 확인 필요 후보만 남기는 WHERE 조건. 최종 판정은 judge()로 한다."""
    return sa.not_(_fails(group, column, min_confidence))


# --- 3상태 판정 ---


class least(GenericFunction):  # noqa: N801 - SQL 함수 이름
    inherit_cache = True


class greatest(GenericFunction):  # noqa: N801
    inherit_cache = True


@compiles(least, "sqlite")
def _least_sqlite(element, compiler, **kw):
    return f"min({compiler.process(element.clauses, **kw)})"  # 인자 둘 이상이면 스칼라 함수


@compiles(greatest, "sqlite")
def _greatest_sqlite(element, compiler, **kw):
    return f"max({compiler.process(element.clauses, **kw)})"


def _combine(fn, parts: list[ColumnElement]) -> ColumnElement:
    return parts[0] if len(parts) == 1 else fn(*parts)


def _rule_tri(rule: Rule, column: Column, min_confidence: float) -> ColumnElement:
    if rule.confidence < min_confidence:
        return sa.literal(0)
    col = column(rule.attribute)
    return sa.case((col.is_(None), 0), (_rule_condition(rule, col), 1), else_=-1)


def tri_state(group: RuleGroup, column: Column, *, min_confidence: float = 0.7) -> ColumnElement:
    """judge()와 같은 결과를 1 / 0 / -1로 내는 SQL 식."""
    parts = [
        tri_state(n, column, min_confidence=min_confidence)
        if isinstance(n, RuleGroup)
        else _rule_tri(n, column, min_confidence)
        for n in group.rules
    ]
    if not parts:
        # 규칙 없는 그룹: 직접 확인 조건이 있는 any_of만 비해당, 나머지는 확인 필요 (judge와 같다)
        return sa.literal(-1 if group.manual and group.group == "any_of" else 0)
    result = _combine(least if group.group == "all_of" else greatest, parts)
    return least(result, sa.literal(0)) if group.manual else result


def result_label(tri: ColumnElement) -> ColumnElement:
    return sa.case((tri == 1, "eligible"), (tri == -1, "ineligible"), else_="unknown")


def _rule_outcome(rule: Rule, column: Column, min_confidence: float) -> ColumnElement:
    if rule.confidence < min_confidence:
        return sa.literal("low_confidence")
    col = column(rule.attribute)
    return sa.case((col.is_(None), "missing"), (_rule_condition(rule, col), "pass"), else_="fail")


def _t(v) -> ColumnElement:
    if isinstance(v, ColumnElement):
        return sa.cast(v, sa.Text)
    return sa.cast(sa.literal(v) if v is not None else sa.null(), sa.Text)


def reasons_json(
    group: RuleGroup, column: Column, actual: Column, *, min_confidence: float = 0.7
) -> ColumnElement:
    """judge()의 근거 목록과 같은 순서의 JSONB 배열 (PostgreSQL)."""
    items: list[ColumnElement] = []

    def obj(rule_id, outcome, detail, source, value=None) -> ColumnElement:
        # jsonb_build_object는 인자 타입을 추론하지 못하므로 글자 값은 모두 text로 못박는다.
        return sa.func.jsonb_build_object(
            _t("rule_id"), _t(rule_id), _t("outcome"), _t(outcome), _t("detail"), _t(detail),
            _t("source"), _t(source), _t("actual"), value if value is not None else sa.null(),
        )  # fmt: skip

    def walk(g: RuleGroup) -> None:
        for n in g.rules:
            if isinstance(n, RuleGroup):
                walk(n)
            else:
                items.append(
                    obj(
                        n.id,
                        _rule_outcome(n, column, min_confidence),
                        f"{n.attribute} {n.op} {n.value!r}",
                        n.source,
                        actual(n.attribute),
                    )
                )
        if not g.rules and not g.manual:
            items.append(
                obj(None, "missing", "자격 규칙이 하나도 없음 (공고 원문 확인 필요)", None)
            )
        for m in g.manual:
            items.append(obj(None, "manual", f"직접 확인 필요: {m.text}", m.source))

    walk(group)
    if not items:
        return sa.cast(sa.literal("[]"), JSONB)
    # 함수 인자는 100개까지라 나눠 만들고 잇는다.
    chunks = [sa.func.jsonb_build_array(*items[i : i + 90]) for i in range(0, len(items), 90)]
    out = chunks[0]
    for c in chunks[1:]:
        out = out.op("||")(c)
    return out


def jsonb_attribute(attributes: ColumnElement, attr: str) -> ColumnElement:
    """프로필 JSONB에서 속성 값을 규칙 타입으로 읽는다. 읽을 수 없으면 NULL(확인 필요).

    judge()의 형 변환과 같다: 숫자 속성은 숫자이거나 쉼표·앞뒤 공백을 뗀 숫자 문자열만,
    참/거짓 속성은 true/false와 "예"·"아니오" 같은 낱말만 받는다.
    """
    j = sa.type_coerce(attributes, JSONB)[attr]
    text = j.astext
    kind = sa.func.jsonb_typeof(j)
    expected = ATTRIBUTES[attr]
    if expected is bool:
        word = sa.func.lower(sa.func.btrim(text, " \t\n\r\f\v"))
        return sa.case(
            (kind.in_(["boolean", "number", "string"]) & word.in_(TRUE_WORDS), sa.true()),
            (kind.in_(["boolean", "number", "string"]) & word.in_(FALSE_WORDS), sa.false()),
            else_=sa.null(),
        )
    if expected is int:
        cleaned = sa.func.btrim(sa.func.replace(text, ",", ""), " \t\n\r\f\v")
        return sa.case(
            (kind == "number", sa.cast(text, sa.Numeric)),
            (
                (kind == "string") & cleaned.regexp_match(f"^{NUMBER.pattern}$"),
                sa.cast(cleaned, sa.Numeric),
            ),
            else_=sa.null(),
        )
    return sa.case((kind.in_(["string", "number", "boolean"]), text), else_=sa.null())


def bulk_judge(
    session: Session,
    *,
    program_id: str,
    program_version_id: str,
    group: RuleGroup,
    min_confidence: float = 0.7,
) -> int:
    """profile 전체를 SQL 한 문장으로 판정해 judgment에 쌓는다 (PostgreSQL). 넣은 행 수를 준다.

    같은 사용자·사업 버전·규칙 해시의 판정이 이미 있으면 건너뛴다(다시 돌려도 같다).
    """
    from noticedelta.db.models import JudgmentRow, Profile  # 순환 import를 피한다

    p = Profile.__table__
    attrs = p.c.attributes

    def column(attr: str) -> ColumnElement:
        return jsonb_attribute(attrs, attr)

    def actual(attr: str) -> ColumnElement:
        return sa.type_coerce(attrs, JSONB)[attr]

    h = ruleset_hash(group)
    tri = tri_state(group, column, min_confidence=min_confidence)
    key = p.c.user_id + f":{program_version_id}:{h}"
    select = sa.select(
        sa.func.md5(key),
        p.c.user_id,
        _t(program_id),
        _t(program_version_id),
        _t(h),
        _t(result_label(tri)),
        reasons_json(group, column, actual, min_confidence=min_confidence),
        sa.func.now(),
    )
    j = JudgmentRow.__table__
    stmt = (
        pg_insert(j)
        .from_select(
            [
                "id",
                "user_id",
                "program_id",
                "program_version_id",
                "ruleset_hash",
                "result",
                "reasons",
                "created_at",
            ],
            select,
        )  # fmt: skip
        .on_conflict_do_nothing(
            index_elements=["user_id", "program_id", "program_version_id", "ruleset_hash"]
        )
    )
    # SQLAlchemy 2.1의 INSERT ... SELECT는 rowcount를 주지 않아 RETURNING으로 센다.
    return len(session.execute(stmt.returning(j.c.id)).all())
