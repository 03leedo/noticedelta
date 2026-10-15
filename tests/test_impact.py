import itertools

from noticedelta.eligibility import Judgment
from noticedelta.impact import (
    FieldValue,
    Subscription,
    build_field_source_index,
    keyword_candidates,
    run_impact,
)
from noticedelta.matching import match_segments
from noticedelta.segments import segment_document

RULES_84 = {
    "group": "all_of",
    "rules": [{"id": "age", "attribute": "business_age_months", "op": "<=", "value": 84}],
}
RULES_60 = {
    "group": "all_of",
    "rules": [{"id": "age", "attribute": "business_age_months", "op": "<=", "value": 60}],
}

OLD_DOC = [
    "□ 사업 개요\n\n(가상) 2026년 지역 창업 지원사업\n\n"
    "○ 지원대상: 창업 7년 이내 기업\n\n"
    "○ 지원규모: 기업당 최대 5천만원\n\n"
    "○ 접수 마감: 2026년 10월 31일 18:00\n\n"
    "○ 제출서류: 사업계획서 1부"
]


class FakeExtractor:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, fields, segments):
        self.calls.append((set(fields), [s.lineage_id for s in segments]))
        return {f: self.results[f] for f in fields if f in self.results}


def setup_versions(new_doc):
    counter = itertools.count(1)
    new_id = lambda: f"L{next(counter)}"  # noqa: E731
    old = match_segments([], segment_document(OLD_DOC, source="body"), new_id=new_id).segments
    lid = {s.text: s.lineage_id for s in old}
    old_fields = {
        "eligibility_rules": FieldValue(
            "eligibility_rules", "rules", RULES_84, [lid["○ 지원대상: 창업 7년 이내 기업"]]
        ),
        "amount": FieldValue(
            "amount", "amount", 50_000_000, [lid["○ 지원규모: 기업당 최대 5천만원"]]
        ),
        "deadline": FieldValue(
            "deadline", "datetime", "2026-10-31 18:00", [lid["○ 접수 마감: 2026년 10월 31일 18:00"]]
        ),
        "required_documents": FieldValue(
            "required_documents", "list", ["사업계획서 1부"], [lid["○ 제출서류: 사업계획서 1부"]]
        ),
    }
    change = match_segments(old, segment_document(new_doc, source="body"), new_id=new_id)
    return old_fields, change


PROFILES = {"A": {"business_age_months": 72}, "B": {"business_age_months": 36}, "C": {}}
OLD_JUDGMENTS = {"A": Judgment("eligible"), "B": Judgment("eligible"), "C": Judgment("unknown")}
SUBS = [
    Subscription("A", {"deadline"}),
    Subscription("B", {"deadline", "amount"}),
    Subscription("C", set()),
]


def run(new_doc, results, **kw):
    old_fields, change = setup_versions(new_doc)
    ex = FakeExtractor(results)
    res = run_impact(
        notice_id="N1",
        to_version_id="V2",
        change=change,
        old_fields=old_fields,
        extractor=ex,
        subscriptions=SUBS,
        profiles=PROFILES,
        old_judgments=OLD_JUDGMENTS,
        **kw,
    )
    return res, ex


def test_eligibility_change_alerts_only_users_whose_judgment_changed():
    new_doc = [OLD_DOC[0].replace("창업 7년 이내", "창업 5년 이내")]
    res, ex = run(
        new_doc, {"eligibility_rules": FieldValue("eligibility_rules", "rules", RULES_60, ["L3"])}
    )
    assert res.affected_fields == {"eligibility_rules"}
    assert res.real_changes == {"eligibility_rules"}
    assert [(a.user_id, a.kind, a.before, a.after) for a in res.alerts] == [
        ("A", "judgment_changed", "eligible", "ineligible")
    ]
    assert res.alerts[0].idempotency_key == "A:N1:eligibility_rules:V2"


def test_wording_only_change_makes_no_alert():
    new_doc = [OLD_DOC[0].replace("2026년 10월 31일 18:00", "2026. 10. 31.(금) 18:00")]
    res, ex = run(
        new_doc, {"deadline": FieldValue("deadline", "datetime", "2026. 10. 31. 18:00", ["L6"])}
    )
    assert res.affected_fields == {"deadline"}
    assert res.real_changes == set()
    assert res.alerts == []


def test_deadline_extension_alerts_deadline_subscribers():
    new_doc = [OLD_DOC[0].replace("2026년 10월 31일 18:00", "2026년 11월 7일 18:00")]
    res, ex = run(
        new_doc, {"deadline": FieldValue("deadline", "datetime", "2026-11-07 18:00", ["L6"])}
    )
    assert res.real_changes == {"deadline"}
    assert sorted((a.user_id, a.kind) for a in res.alerts) == [
        ("A", "field_changed"),
        ("B", "field_changed"),
    ]
    assert res.alerts[0].after == "2026-11-07 18:00"


def test_no_change_skips_extractor():
    res, ex = run(OLD_DOC, {})
    assert ex.calls == []
    assert res.llm_fields_requested == 0
    assert res.alerts == []


def test_extractor_gets_only_affected_fields_and_related_segments():
    new_doc = [OLD_DOC[0].replace("창업 7년 이내", "창업 5년 이내")]
    res, ex = run(
        new_doc, {"eligibility_rules": FieldValue("eligibility_rules", "rules", RULES_60, ["L3"])}
    )
    fields, lineages = ex.calls[0]
    assert fields == {"eligibility_rules"}
    assert lineages == ["L3"]
    assert res.llm_fields_requested == 1


def test_added_paragraph_uses_keyword_candidates():
    new_doc = [OLD_DOC[0] + "\n\n○ 추가 제출서류: 사업자등록증 사본"]
    res, ex = run(
        new_doc,
        {
            "required_documents": FieldValue(
                "required_documents", "list", ["사업계획서 1부", "사업자등록증 사본"], ["L7", "L8"]
            )
        },
    )
    assert res.affected_fields == {"required_documents"}
    assert res.real_changes == {"required_documents"}
    assert res.alerts == []  # 서류를 구독한 사용자 없음
    assert res.fields["required_documents"].value == ["사업계획서 1부", "사업자등록증 사본"]


def test_field_source_index_and_keyword_candidates():
    fields = {
        "deadline": FieldValue("deadline", "datetime", "x", ["L1", "L2"]),
        "amount": FieldValue("amount", "amount", 1, ["L2"]),
    }
    assert build_field_source_index(fields) == {"L1": {"deadline"}, "L2": {"deadline", "amount"}}
    seg = segment_document(["○ 신청기간: 2026. 10. 1. ~ 10. 31. / 지원금 최대 1억원"], source="b")[
        0
    ]
    assert {"deadline", "amount"} <= keyword_candidates(seg)
