import json

from noticedelta.llm import FileCassetteStore, Gateway, LLMResponse, Mode, request_key
from noticedelta.llm.extract import (
    FIELD_TYPES,
    TEMPLATE_V1,
    TEMPLATE_V2,
    LLMExtractor,
    build_request,
)
from noticedelta.matching import match_segments
from noticedelta.segments import Segment, segment_document


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
    # 기본 틀(v2)은 조각 ID 대신 요청 안의 번호를 쓴다. 답의 번호는 lineage_id로 되돌린다
    assert "[2] ○ 접수 마감: 2026년 10월 31일 18:00" in req.input
    assert req.output_schema["properties"]["fields"]["items"]["properties"]["name"]["enum"] == [
        "deadline"
    ]


def test_verified_values_keep_full_confidence(tmp_path):
    ex, _ = extractor(
        [item("deadline", "2026-10-31 18:00", ["2"]), item("amount", 50_000_000, ["3"])], tmp_path
    )
    out = ex({"deadline", "amount"}, segments())
    assert out["deadline"].value_type == FIELD_TYPES["deadline"] == "datetime"
    assert out["deadline"].confidence == 1.0
    assert out["amount"].sources == ["L3"]


def test_unverified_value_gets_low_confidence(tmp_path):
    ex, _ = extractor([item("deadline", "2026-11-30", ["2"])], tmp_path)
    assert ex({"deadline"}, segments())["deadline"].confidence == 0.3


def test_unknown_sources_and_unrequested_fields_are_dropped(tmp_path):
    ex, _ = extractor(
        [item("deadline", "2026-10-31 18:00", ["2", "99"]), item("amount", 1, ["3"])], tmp_path
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
    ex, _ = extractor([item("eligibility_rules", rules, ["1"])], tmp_path)
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
    ex, _ = extractor([item("eligibility_rules", bad, ["1"])], tmp_path)
    assert ex({"eligibility_rules"}, segments()) == {}


def test_empty_request_makes_no_call(tmp_path):
    ex, p = extractor([], tmp_path)
    assert ex(set(), segments()) == {}
    assert p.requests == []


def test_null_or_misshapen_values_are_treated_as_absent(tmp_path):
    # 없는 필드를 빼지 않고 null로 주는 모델이 있다(피클 학교 모델). 값 없음으로 본다
    ex, _ = extractor(
        [
            item("required_documents", None, []),
            item("contact", ["02-1"], ["1"]),
            item("amount", 50_000_000, ["3"]),
        ],
        tmp_path,
    )
    out = ex({"required_documents", "contact", "amount"}, segments())
    assert set(out) == {"amount"}


def test_v1_requests_stay_byte_identical_so_old_cassettes_replay():
    # v2를 더하면서 v1 요청이 한 글자라도 바뀌면 지난 실측(녹화본 90개)을 다시 돌려야 한다
    req = build_request(set(FIELD_TYPES), segments(), model="pickle-general", template=TEMPLATE_V1)
    assert "[L2] ○ 접수 마감: 2026년 10월 31일 18:00" in req.input
    assert request_key(req) == "03026922afc3c01c3895518578688216ad462e0ddf0d8d1663b10d60f799faeb"


def test_v2_numbers_lines_skips_empty_segments_and_maps_sources_back(tmp_path):
    segs = segments()
    blank = Segment("table_cell", "  ", "h-blank", {}, 9, lineage_id="L9")
    segs = [segs[0], blank, *segs[1:]]
    req = build_request({"deadline"}, segs, template=TEMPLATE_V2)
    assert "[2] ○ 접수 마감: 2026년 10월 31일 18:00" in req.input
    assert "L2" not in req.input and "L9" not in req.input  # 긴 ID도 빈 조각도 보내지 않는다
    assert "줄의 번호" in req.system

    # 모델이 번호를 "[2]"나 숫자로 줘도 받는다. 저장되는 근거는 lineage_id다
    ex, _ = extractor(
        [
            item("deadline", "2026-10-31 18:00", ["[2]"]),
            item("amount", 50_000_000, [3]),
            item("contact", "02-1", ["99"]),  # 없는 번호는 근거로 치지 않는다
        ],
        tmp_path,
    )
    out = ex({"deadline", "amount", "contact"}, segs)
    assert out["deadline"].sources == ["L2"] and out["deadline"].confidence == 1.0
    assert out["amount"].sources == ["L3"] and out["amount"].confidence == 1.0
    assert out["contact"].sources == [] and out["contact"].confidence < 1.0


def test_only_blank_segments_make_no_call(tmp_path):
    ex, p = extractor([], tmp_path)
    blank = Segment("table_cell", "", "h", {}, 0, lineage_id="L1")
    assert ex({"deadline"}, [blank]) == {}
    assert p.requests == []


def test_bare_string_values_are_read_as_text(tmp_path):
    # JSON 문자열로 감싸라고 해도 맨 글자를 넣는 모델이 있다. 버리면 있는 마감일을 놓친다
    ex, _ = extractor(
        [
            {"name": "deadline", "value_json": "2026-10-31 18:00", "sources": [2]},
            {"name": "eligibility_rules", "value_json": "창업 7년 이내", "sources": [1]},
        ],
        tmp_path,
    )
    out = ex({"deadline", "eligibility_rules"}, segments())
    assert out["deadline"].value == "2026-10-31 18:00" and out["deadline"].confidence == 1.0
    assert "eligibility_rules" not in out  # 규칙은 객체여야 한다
