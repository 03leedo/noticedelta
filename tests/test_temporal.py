from datetime import date

import pytest

from noticedelta.eligibility import RuleGroup, judge
from noticedelta.eligibility.temporal import (
    add_months,
    age_on,
    derive_profile,
    eligibility_timeline,
    months_between,
    near_misses,
)


def group(*rules, kind="all_of"):
    return RuleGroup.model_validate({"group": kind, "rules": list(rules)})


AGE7 = {"id": "r_age", "attribute": "business_age_months", "op": "<=", "value": 84, "source": "L1"}
CEO39 = {"id": "r_ceo", "attribute": "ceo_age", "op": "<=", "value": 39, "source": "L2"}
REGION = {"id": "r_reg", "attribute": "region_sido", "op": "==", "value": "부산", "source": "L3"}
REVENUE = {"id": "r_rev", "attribute": "revenue_krw", "op": "<=", "value": 1_000_000_000}


@pytest.mark.parametrize(
    ("start", "on", "months"),
    [
        (date(2019, 12, 1), date(2026, 10, 18), 82),
        (date(2019, 12, 1), date(2026, 12, 1), 84),
        (date(2019, 12, 1), date(2026, 11, 30), 83),
        (date(2026, 1, 31), date(2026, 2, 28), 1),  # 말일 개업은 다음 달 말일에 1개월
        (date(2026, 1, 31), date(2026, 2, 27), 0),
    ],
)
def test_months_between(start, on, months):
    assert months_between(start, on) == months
    if months:
        assert months_between(start, add_months(start, months)) == months


def test_age_on_including_leap_day():
    assert age_on(date(1990, 5, 1), date(2026, 4, 30)) == 35
    assert age_on(date(1990, 5, 1), date(2026, 5, 1)) == 36
    assert age_on(date(2004, 2, 29), date(2026, 2, 28)) == 21
    assert age_on(date(2004, 2, 29), date(2026, 3, 1)) == 22


def test_derive_profile_fills_age_from_dates():
    p = derive_profile(
        {"founded_on": "2019-12-01", "ceo_birth_date": "1990-05-01"}, date(2026, 10, 18)
    )
    assert (p["business_age_months"], p["ceo_age"]) == (82, 36)


def test_timeline_finds_the_day_eligibility_is_lost():
    g = group(AGE7, REGION)
    profile = {"founded_on": "2019-12-01", "region_sido": "부산"}
    t = eligibility_timeline(g, profile, start=date(2026, 10, 18))
    assert t.current.result == "eligible"
    ch = t.next_change
    assert (ch.on, ch.before, ch.after) == (date(2027, 1, 1), "eligible", "ineligible")
    assert [r.rule_id for r in ch.reasons] == ["r_age"]
    assert len(t.changes) == 1


def test_timeline_stops_at_deadline():
    g = group(AGE7)
    t = eligibility_timeline(
        g, {"founded_on": "2019-12-01"}, start=date(2026, 10, 18), until=date(2026, 12, 31)
    )
    assert t.changes == []  # 마감 전에는 바뀌지 않는다


def test_timeline_finds_gain_too():
    g = group({"attribute": "ceo_age", "op": ">=", "value": 20})
    t = eligibility_timeline(g, {"ceo_birth_date": "2007-03-15"}, start=date(2026, 10, 18))
    assert t.current.result == "ineligible"
    assert (t.next_change.on, t.next_change.after) == (date(2027, 3, 15), "eligible")


def test_timeline_two_rules_two_changes():
    g = group(AGE7, CEO39)
    profile = {"founded_on": "2026-01-01", "ceo_birth_date": "1987-02-10"}
    t = eligibility_timeline(g, profile, start=date(2026, 10, 18), horizon_days=400)
    assert [(c.on, c.after) for c in t.changes] == [(date(2027, 2, 10), "ineligible")]


def test_fixed_reference_date_means_no_changes():
    g = group(AGE7)
    t = eligibility_timeline(
        g, {"founded_on": "2019-12-01"}, start=date(2026, 10, 18), fixed_on=date(2026, 10, 1)
    )
    assert t.as_of == date(2026, 10, 1) and t.changes == []


def test_timeline_without_dates_is_static():
    g = group(AGE7)
    t = eligibility_timeline(g, {"business_age_months": 30}, start=date(2026, 10, 18))
    assert t.current.result == "eligible" and t.changes == []


def test_near_miss_single_failing_rule_with_gap():
    g = group(REGION, REVENUE)
    misses = near_misses(g, {"region_sido": "부산", "revenue_krw": "1,020,000,000"})
    assert [(m.rule.id, m.result_if_met, m.gap) for m in misses] == [
        ("r_rev", "eligible", 20_000_000)
    ]


def test_two_failures_are_not_near_misses():
    g = group(REGION, REVENUE)
    assert near_misses(g, {"region_sido": "서울", "revenue_krw": 2_000_000_000}) == []


def test_near_miss_may_lead_to_unknown():
    g = group(REGION, CEO39)
    misses = near_misses(g, {"region_sido": "서울"})  # 나이 정보 없음
    assert [(m.rule.id, m.result_if_met) for m in misses] == [("r_reg", "unknown")]


def test_assume_pass_only_affects_given_rule():
    g = group(REGION, REVENUE)
    profile = {"region_sido": "서울", "revenue_krw": 2_000_000_000}
    assert judge(g, profile, assume_pass=[g.rules[0]]).result == "ineligible"
    assert judge(g, profile, assume_pass=g.rules).result == "eligible"
