import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from noticedelta.eligibility import RuleGroup, judge, to_sql_where

BASE = {
    "group": "all_of",
    "rules": [
        {"id": "r1", "attribute": "business_age_months", "op": "<=", "value": 84, "source": "L42"},
        {
            "id": "r2",
            "attribute": "region_sido",
            "op": "not_in",
            "value": ["서울", "경기", "인천"],
            "source": "L43",
        },
    ],
}


def rules(**overrides):
    data = {**BASE, **overrides}
    return RuleGroup.model_validate(data)


def test_all_rules_pass_is_eligible():
    j = judge(rules(), {"business_age_months": 72, "region_sido": "부산"})
    assert j.result == "eligible"
    assert [r.outcome for r in j.reasons] == ["pass", "pass"]


def test_failed_rule_is_ineligible_with_source():
    g = rules(
        rules=[
            {
                "id": "r1",
                "attribute": "business_age_months",
                "op": "<=",
                "value": 60,
                "source": "L42",
            }
        ]
    )
    j = judge(g, {"business_age_months": 72})
    assert j.result == "ineligible"
    assert j.reasons[0].outcome == "fail"
    assert j.reasons[0].source == "L42"
    assert j.reasons[0].rule_id == "r1"


def test_missing_profile_value_is_unknown():
    j = judge(rules(), {"region_sido": "부산"})
    assert j.result == "unknown"
    assert j.reasons[0].outcome == "missing"


def test_missing_value_but_other_rule_fails_is_ineligible():
    j = judge(rules(), {"region_sido": "서울"})
    assert j.result == "ineligible"


def test_low_confidence_rule_is_unknown():
    g = rules(
        rules=[
            {
                "id": "r1",
                "attribute": "business_age_months",
                "op": "<=",
                "value": 84,
                "confidence": 0.5,
            }
        ]
    )
    j = judge(g, {"business_age_months": 72})
    assert j.result == "unknown"
    assert j.reasons[0].outcome == "low_confidence"


def test_manual_condition_downgrades_eligible_to_unknown():
    g = rules(manual=[{"text": "타 정부지원사업 중복 수혜 불가", "source": "L51"}])
    j = judge(g, {"business_age_months": 72, "region_sido": "부산"})
    assert j.result == "unknown"
    assert any(r.outcome == "manual" and r.source == "L51" for r in j.reasons)


def test_manual_condition_does_not_rescue_failure():
    g = rules(manual=[{"text": "기타 조건"}])
    j = judge(g, {"business_age_months": 100, "region_sido": "부산"})
    assert j.result == "ineligible"


def test_any_of_group():
    g = RuleGroup.model_validate(
        {
            "group": "any_of",
            "rules": [
                {"id": "a", "attribute": "is_pre_founder", "op": "==", "value": True},
                {"id": "b", "attribute": "business_age_months", "op": "<=", "value": 36},
            ],
        }
    )
    assert judge(g, {"is_pre_founder": False, "business_age_months": 12}).result == "eligible"
    assert judge(g, {"is_pre_founder": False, "business_age_months": 50}).result == "ineligible"
    assert judge(g, {"is_pre_founder": False}).result == "unknown"


def test_nested_groups_and_between():
    g = RuleGroup.model_validate(
        {
            "group": "all_of",
            "rules": [
                {"id": "age", "attribute": "ceo_age", "op": "between", "value": [19, 39]},
                {
                    "group": "any_of",
                    "rules": [
                        {"id": "w", "attribute": "is_women_owned", "op": "==", "value": True},
                        {"id": "r", "attribute": "is_restart", "op": "==", "value": True},
                    ],
                },
            ],
        }
    )
    assert (
        judge(g, {"ceo_age": 39, "is_women_owned": True, "is_restart": False}).result == "eligible"
    )
    assert judge(g, {"ceo_age": 40, "is_women_owned": True}).result == "ineligible"


def test_unknown_attribute_rejected():
    with pytest.raises(ValidationError):
        RuleGroup.model_validate(
            {
                "group": "all_of",
                "rules": [{"id": "x", "attribute": "favorite_color", "op": "==", "value": "blue"}],
            }
        )


def test_in_requires_list():
    with pytest.raises(ValidationError):
        RuleGroup.model_validate(
            {
                "group": "all_of",
                "rules": [{"id": "x", "attribute": "region_sido", "op": "in", "value": "서울"}],
            }
        )


def test_sql_where_keeps_eligible_and_unknown_only():
    engine = sa.create_engine("sqlite://")
    md = sa.MetaData()
    profiles = sa.Table(
        "profile",
        md,
        sa.Column("user_id", sa.String, primary_key=True),
        sa.Column("business_age_months", sa.Integer),
        sa.Column("region_sido", sa.String),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            profiles.insert(),
            [
                {"user_id": "A", "business_age_months": 72, "region_sido": "부산"},
                {"user_id": "B", "business_age_months": 72, "region_sido": "서울"},
                {"user_id": "C", "business_age_months": None, "region_sido": "대구"},
                {"user_id": "D", "business_age_months": 100, "region_sido": None},
            ],
        )
        where = to_sql_where(rules(), lambda attr: profiles.c[attr])
        got = sorted(r.user_id for r in conn.execute(sa.select(profiles.c.user_id).where(where)))
    assert got == ["A", "C"]


def test_wrong_profile_type_is_unknown_not_crash():
    j = judge(rules(), {"business_age_months": "72개월", "region_sido": "부산"})
    assert j.result == "unknown"
    assert j.reasons[0].outcome == "missing"
