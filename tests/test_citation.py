from datetime import datetime
from zoneinfo import ZoneInfo

from noticedelta.citation import verify_citation, verify_rule
from noticedelta.eligibility import Rule

KST = ZoneInfo("Asia/Seoul")


def test_deadline_found_in_cited_text():
    texts = ["접수기간: 2026. 10. 13.(월) ~ 2026. 10. 31.(금) 18:00까지"]
    assert verify_citation("datetime", datetime(2026, 10, 31, 18, 0, tzinfo=KST), texts)
    assert verify_citation("datetime", "2026-10-31 18:00", texts)


def test_deadline_not_in_cited_text():
    texts = ["접수기간: 2026. 10. 13.(월) ~ 2026. 10. 31.(금) 18:00까지"]
    assert not verify_citation("datetime", "2026-11-07", texts)


def test_date_only_value_matches_text_with_time():
    texts = ["마감: 2026. 10. 31.(금) 18:00"]
    assert verify_citation("datetime", "2026-10-31", texts)


def test_amount_across_formats():
    assert verify_citation("amount", "1억원", ["기업당 100,000,000원 이내"])
    assert not verify_citation("amount", 50_000_000, ["기업당 최대 1억원"])


def test_rule_months_found_as_years():
    rule = Rule(id="r", attribute="business_age_months", op="<=", value=84)
    assert verify_rule(rule, ["공고일 기준 창업 7년 이내 기업"])
    assert not verify_rule(rule, ["공고일 기준 창업 5년 이내 기업"])


def test_rule_string_values_must_appear():
    rule = Rule(id="r", attribute="region_sido", op="not_in", value=["서울", "경기"])
    assert verify_rule(rule, ["수도권(서울, 경기) 소재 기업 제외"])
    assert not verify_rule(rule, ["비수도권 기업"])


def test_rules_value_requires_every_rule():
    value = {
        "group": "all_of",
        "rules": [
            {"id": "a", "attribute": "ceo_age", "op": "<=", "value": 39},
            {"id": "b", "attribute": "business_age_months", "op": "<=", "value": 84},
        ],
    }
    assert verify_citation("rules", value, ["만 39세 이하", "창업 7년 이내"])
    assert not verify_citation("rules", value, ["만 39세 이하"])


def test_text_is_normalized_substring():
    assert verify_citation("text", "사업계획서  1부", ["제출서류: 사업계획서 1부, 사업자등록증"])
    assert verify_citation(
        "list", ["사업계획서 1부", "사업자등록증"], ["제출서류: 사업계획서 1부, 사업자등록증"]
    )
    assert not verify_citation("list", ["재무제표"], ["제출서류: 사업계획서 1부"])
