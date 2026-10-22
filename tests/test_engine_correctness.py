"""평가 문서에서 재현된 엔진·판정 결함의 회귀 테스트."""

import itertools
import json
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from noticedelta.citation import verify_rule
from noticedelta.eligibility import Judgment, Rule, RuleGroup, judge
from noticedelta.eligibility.temporal import derive_profile, near_misses
from noticedelta.impact import FieldValue, keyword_candidates, run_impact
from noticedelta.llm.cassette import FileCassetteStore
from noticedelta.matching import match_segments
from noticedelta.segments import Table, segment_document
from noticedelta.values import find_datetimes, parse_kst_datetime

# --- 빈 규칙과 규칙 검증 (I3) ---


@pytest.mark.parametrize("kind", ["all_of", "any_of"])
def test_empty_rule_group_is_unknown_not_eligible(kind):
    j = judge(RuleGroup(group=kind), {"ceo_age": 50})
    assert j.result == "unknown"
    assert "규칙이 하나도 없음" in j.reasons[0].detail


def test_nested_empty_group_does_not_pass():
    g = RuleGroup.model_validate({"group": "all_of", "rules": [{"group": "any_of", "rules": []}]})
    assert judge(g, {}).result == "unknown"


def rule(attr, op, value):
    return Rule(attribute=attr, op=op, value=value)


@pytest.mark.parametrize(
    ("r", "cited", "ok"),
    [
        (rule("ceo_age", "<=", 39), "대표자 만 39세 이하", True),
        (rule("ceo_age", ">=", 39), "대표자 만 39세 이하", False),  # 방향이 거꾸로
        (rule("ceo_age", ">", 39), "만 39세 초과자 제외", True),  # 방향 말 '초과'와 일치
        (rule("business_age_months", "<=", 84), "창업 7년 이내 기업", True),
        (rule("business_age_months", ">=", 84), "창업 7년 이내 기업", False),
        (rule("business_age_months", ">=", 31), "접수 마감 2026년 10월 31일 18:00", False),
        (rule("ceo_age", ">=", 18), "접수 마감 2026년 10월 31일 18:00", False),  # 단위 '세' 없음
        (rule("employees", "<=", 5), "상시근로자 5인 미만", True),
        (rule("ceo_age", "<=", 39), "만 39세", True),  # 방향 말이 없으면 방향은 안 따진다
    ],
)
def test_rule_citation_checks_unit_and_direction(r, cited, ok):
    assert verify_rule(r, [cited]) is ok


def test_rule_value_type_must_match_attribute():
    with pytest.raises(ValidationError):
        rule("ceo_age", "<=", "abc")
    with pytest.raises(ValidationError):
        rule("is_pre_founder", "==", 1)
    rule("region_sido", "in", ["부산", "울산"])  # 문자열 목록은 그대로


# --- 연도 없는 날짜 회귀 (#5) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("마감 12. 31.(수) 24:00", [datetime(2026, 12, 31, 23, 59)]),
        (
            "접수기간 10. 1.(수) ~ 10. 31.(금)",
            [datetime(2026, 10, 1, 23, 59), datetime(2026, 10, 31, 23, 59)],
        ),
        (
            "신청기간 10. 13. ~ 10. 31.",
            [datetime(2026, 10, 13, 23, 59), datetime(2026, 10, 31, 23, 59)],
        ),
    ],
)
def test_yearless_dates_after_a_space_are_found(text, expected):
    got = [d.replace(tzinfo=None) for d in find_datetimes(text, default_year=2026)]
    assert got == expected


def test_list_numbers_still_not_dates():
    assert parse_kst_datetime("1. 2. 신청 자격은 다음과 같다", default_year=2026) is None


# --- 다른 표끼리 짝짓기 (#4) ---


def att_segments(tables_by_file, ids):
    """첨부마다 따로 조각화하고 version_segments처럼 파일 이름·역할을 붙인다."""
    out = []
    for filename, rows in tables_by_file:
        segs = segment_document([Table(rows)], source=f"att:{filename}", start_order=len(out))
        out += [
            s.__class__(
                s.kind,
                s.text,
                s.text_hash,
                {**s.locator, "filename": filename, "role": "attachment"},
                s.order,
            )
            for s in segs
        ]
    return match_segments([], out, new_id=ids).segments if ids else out


def test_tables_with_same_header_in_different_attachments_are_not_paired():
    ids = (lambda c: lambda: f"L{next(c)}")(itertools.count(1))
    head = ["구분", "내용"]
    old = att_segments(
        [
            ("붙임1 신청서.hwp", [head, ["지역", "부산"], ["업력", "7년"]]),
            ("붙임2 서류.hwp", [head, ["서류", "사업계획서"]]),
        ],
        ids,
    )
    new = att_segments(
        [
            ("붙임1 신청서.hwp", [head, ["업력", "7년"]]),
            ("붙임2 서류.hwp", [head, ["서류", "사업계획서"], ["고용", "5인"]]),
        ],
        None,
    )
    cs = match_segments(old, new, new_id=ids)
    assert (len(cs.modified), len(cs.added), len(cs.removed)) == (0, 2, 2)


def test_revision_marks_in_filename_keep_table_pairing():
    ids = (lambda c: lambda: f"L{next(c)}")(itertools.count(1))
    head = ["구분", "내용"]
    old = att_segments([("공고문.hwp", [head, ["업력", "7년 이내"]])], ids)
    new = att_segments([("공고문(수정).hwp", [head, ["업력", "5년 이내"]])], None)
    cs = match_segments(old, new, new_id=ids)
    assert len(cs.modified) == 1 and not cs.added and not cs.removed


# --- 영향 엔진 ---


class Extractor:
    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, fields, segments):
        self.calls.append(set(fields))
        return {f: self.results[f] for f in fields if f in self.results}


RULES = {
    "group": "all_of",
    "rules": [{"attribute": "business_age_months", "op": "<=", "value": 60}],
}


def versions(old_doc, new_doc):
    ids = (lambda c: lambda: f"L{next(c)}")(itertools.count(1))
    old = match_segments([], segment_document(old_doc, source="body"), new_id=ids).segments
    return old, match_segments(old, segment_document(new_doc, source="body"), new_id=ids)


def test_deleted_uncited_clause_is_detected():
    old, change = versions(
        ["○ 지원대상: 창업 5년 이내", "○ 단, 재창업자는 제외 대상"], ["○ 지원대상: 창업 5년 이내"]
    )
    fields = {
        "eligibility_rules": FieldValue("eligibility_rules", "rules", RULES, [old[0].lineage_id])
    }
    ex = Extractor({})
    run_impact(
        notice_id="N",
        to_version_id="v2",
        change=change,
        old_fields=fields,
        extractor=ex,
        subscriptions=[],
        profiles={},
        old_judgments={},
        old_segments=old,
    )
    assert ex.calls and "eligibility_rules" in ex.calls[0]


def test_table_cell_candidates_use_header_and_row_key():
    seg = segment_document([Table([["구분", "업력"], ["예비", "5년"]])], source="b")[-1]
    assert "eligibility_rules" in keyword_candidates(seg)


def test_field_not_found_again_becomes_stale():
    old, change = versions(["○ 접수 마감: 2026년 10월 31일 18:00"], ["○ 문의: 창업지원팀"])
    fields = {
        "deadline": FieldValue("deadline", "datetime", "2026-10-31 18:00", [old[0].lineage_id])
    }
    res = run_impact(
        notice_id="N",
        to_version_id="v2",
        change=change,
        old_fields=fields,
        extractor=Extractor({}),
        subscriptions=[],
        profiles={},
        old_judgments={},
        old_segments=old,
    )
    assert res.stale == {"deadline"} and res.fields["deadline"].confidence < 0.7


def test_newly_eligible_user_without_prior_judgment_is_alerted():
    old, change = versions(["○ 지원대상: 창업 5년 이내"], ["○ 지원대상: 창업 7년 이내"])
    fields = {
        "eligibility_rules": FieldValue("eligibility_rules", "rules", RULES, [old[0].lineage_id])
    }
    relaxed = {
        "group": "all_of",
        "rules": [{"attribute": "business_age_months", "op": "<=", "value": 84}],
    }
    ex = Extractor(
        {
            "eligibility_rules": FieldValue(
                "eligibility_rules", "rules", relaxed, [change.segments[0].lineage_id]
            )
        }
    )
    res = run_impact(
        notice_id="N",
        to_version_id="v2",
        change=change,
        old_fields=fields,
        extractor=ex,
        subscriptions=[],
        profiles={"u1": {"business_age_months": 72}, "u2": {"business_age_months": 100}},
        old_judgments={"u2": Judgment("ineligible")},
    )
    assert [(a.user_id, a.after) for a in res.alerts] == [("u1", "eligible")]


# --- 녹화본, 시간 ---


def test_tampered_cassette_is_ignored(tmp_path):
    store = FileCassetteStore(tmp_path)
    store.save("a" * 64, {"key": "a" * 64, "response": {"x": 1}})
    p = tmp_path / "aa" / f"{'a' * 64}.json"
    p.write_text(json.dumps({"key": "b" * 64, "response": {"x": 2}}), encoding="utf-8")
    assert store.load("a" * 64) is None


@pytest.mark.parametrize(
    "founded", [datetime(2019, 11, 1, 9), "2019.11.01", "2019/11/01", "20191101", "2019-11-01"]
)
def test_founded_on_accepts_common_date_forms(founded):
    p = derive_profile({"founded_on": founded}, date(2026, 10, 1))
    assert p["business_age_months"] == 83


def test_near_misses_use_derived_profile():
    g = RuleGroup.model_validate(
        {
            "group": "all_of",
            "rules": [
                {"id": "age", "attribute": "business_age_months", "op": "<=", "value": 84},
                {"id": "rev", "attribute": "revenue_krw", "op": "<=", "value": 1_000_000_000},
            ],
        }
    )
    profile = {"founded_on": "2010-01-01", "revenue_krw": 2_000_000_000}
    assert near_misses(g, profile, on=date(2026, 10, 1)) == []  # 두 조건 모두 실패
    young = {
        "founded_on": str(date(2026, 10, 1) - timedelta(days=400)),
        "revenue_krw": 2_000_000_000,
    }
    assert [m.rule.id for m in near_misses(g, young, on=date(2026, 10, 1))] == ["rev"]
