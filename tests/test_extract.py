import json

from noticedelta.llm import FileCassetteStore, Gateway, LLMResponse, Mode
from noticedelta.llm.extract import FIELD_TYPES, LLMExtractor, build_request
from noticedelta.matching import match_segments
from noticedelta.segments import segment_document


def segments():
    doc = [
        "○ 지원대상: 창업 7년 이내 기업\n\n○ 접수 마감: 2026년 10월 31일 18:00\n\n○ 지원규모: 최대 5천만원"
    ]
    return match_segments(
        [], segment_document(doc, source="body"), new_id=iter(["L1", "L2", "L3"]).__next__
    ).segments


class ScriptedProvider:
    def __init__(self, items):
        self.items = items
        self.requests = []

    def complete(self, req):
        self.requests.append(req)
        return LLMResponse(
            output={"fields": self.items}, input_tokens=10, output_tokens=5, cost_usd=0.0
        )


def extractor(items, tmp_path):
    p = ScriptedProvider(items)
    return LLMExtractor(Gateway(p, FileCassetteStore(tmp_path), Mode.LIVE)), p


def item(name, value, sources):
    return {"name": name, "value_json": json.dumps(value, ensure_ascii=False), "sources": sources}


def test_request_lists_segments_with_lineage_and_only_requested_fields():
    req = build_request({"deadline"}, segments(), model="claude-haiku-5-5")
    assert "[L2] ○ 접수 마감: 2026년 10월 31일 18:00" in req.input
    assert req.output_schema["properties"]["fields"]["items"]["properties"]["name"]["enum"] == [
        "deadline"
    ]


def test_verified_values_keep_full_confidence(tmp_path):
    ex, _ = extractor(
        [item("deadline", "2026-10-31 18:00", ["L2"]), item("amount", 50_000_000, ["L3"])], tmp_path
    )
    out = ex({"deadline", "amount"}, segments())
    assert out["deadline"].value_type == FIELD_TYPES["deadline"] == "datetime"
    assert out["deadline"].confidence == 1.0
    assert out["amount"].sources == ["L3"]


def test_unverified_value_gets_low_confidence(tmp_path):
    ex, _ = extractor([item("deadline", "2026-11-30", ["L2"])], tmp_path)
    assert ex({"deadline"}, segments())["deadline"].confidence == 0.3


def test_unknown_sources_and_unrequested_fields_are_dropped(tmp_path):
    ex, _ = extractor(
        [item("deadline", "2026-10-31 18:00", ["L2", "L99"]), item("amount", 1, ["L3"])], tmp_path
    )
    out = ex({"deadline"}, segments())
    assert set(out) == {"deadline"}
    assert out["deadline"].sources == ["L2"]


def test_rule_not_found_in_text_gets_low_rule_confidence(tmp_path):
    rules = {
        "group": "all_of",
        "manual": [],
        "rules": [
            {"id": "a", "attribute": "business_age_months", "op": "<=", "value": 84},
            {"id": "b", "attribute": "ceo_age", "op": "<=", "value": 39},
        ],
    }
    ex, _ = extractor([item("eligibility_rules", rules, ["L1"])], tmp_path)
    fv = ex({"eligibility_rules"}, segments())["eligibility_rules"]
    by_id = {r["id"]: r for r in fv.value["rules"]}
    assert by_id["a"]["confidence"] == 1.0
    assert by_id["b"]["confidence"] == 0.3
    assert fv.confidence == 0.3


def test_invalid_rules_are_dropped(tmp_path):
    bad = {
        "group": "all_of",
        "rules": [{"id": "x", "attribute": "favorite_color", "op": "==", "value": "blue"}],
    }
    ex, _ = extractor([item("eligibility_rules", bad, ["L1"])], tmp_path)
    assert ex({"eligibility_rules"}, segments()) == {}


def test_empty_request_makes_no_call(tmp_path):
    ex, p = extractor([], tmp_path)
    assert ex(set(), segments()) == {}
    assert p.requests == []
