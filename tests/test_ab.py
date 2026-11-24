"""추출 틀 비교 (evaluation/ab.py)."""

from noticedelta.evaluation.ab import Side, compare_fields, render_ab, run_ab
from noticedelta.impact import FieldValue
from noticedelta.llm import FileCassetteStore, Gateway, LLMResponse, Mode
from noticedelta.llm.extract import TEMPLATE_V1, TEMPLATE_V2, LLMExtractor
from noticedelta.segments import Segment

TYPES = {"deadline": "datetime", "amount": "amount", "contact": "text"}


def fv(name, value):
    return FieldValue(name, TYPES[name], value, [])


def test_compare_fields_by_type():
    a = {"deadline": fv("deadline", "2026-11-30"), "amount": fv("amount", 50_000_000)}
    b = {"deadline": fv("deadline", "2026-12-01"), "contact": fv("contact", "02-1")}
    assert compare_fields(TYPES, a, b) == {
        "deadline": "differ",
        "amount": "only_a",
        "contact": "only_b",
    }
    assert compare_fields(TYPES, a, a)["deadline"] == "same"


class Answer:
    """틀마다 다른 토큰을 쓰고 같은 값을 답하는 가짜 공급자."""

    def __init__(self, tokens):
        self.tokens = tokens

    def complete(self, req):
        src = "L1" if req.template_version == TEMPLATE_V1 else "1"
        item = {"name": "deadline", "value_json": '"2026-11-30"', "sources": [src]}
        return LLMResponse({"fields": [item]}, self.tokens, 5, 0.0)


def side(tmp_path, template, tokens):
    gw = Gateway(Answer(tokens), FileCassetteStore(tmp_path / template), Mode.LIVE)
    return Side(LLMExtractor(gw, template=template), gw)


def test_run_ab_counts_tokens_per_doc_and_agreement(tmp_path):
    seg = Segment("paragraph", "신청 마감 2026-11-30", "h", {}, 0, lineage_id="L1")
    docs = [("P1", [seg]), ("P2", [seg])]
    res = run_ab(
        docs,
        {"deadline": "datetime"},
        side(tmp_path, TEMPLATE_V1, 100),
        side(tmp_path, TEMPLATE_V2, 60),
    )
    assert res.docs == 2 and res.a_in == [100, 100] and res.b_in == [60, 60]
    assert res.fields["same"] == 2 and res.agreement() == 1.0
    table = render_ab([("v1 대 v2", res)])
    assert "60.0%" in table and "100.0%" in table
