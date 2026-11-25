"""앱 정의로 만드는 추출기 (extraction.py): 지원사업 팩 없이도 앱마다 추출한다."""

import copy

from noticedelta.appspec import check_app
from noticedelta.extraction import (
    TEMPLATE_V1,
    TEMPLATE_V2,
    SpecExtractor,
    build_request,
    spec_for_app,
    system_prompt,
)
from noticedelta.grants import APP
from noticedelta.llm import LLMResponse, request_key
from noticedelta.llm.extract import (
    _SYSTEM,
    ATTRIBUTES,
    DOMAIN,
    GRANTS_SPEC,
    NOTES,
    spec_for,
)
from noticedelta.matching import match_segments
from noticedelta.segments import segment_document

# app-spec 5장의 약국 앱(가상). 규칙 필드가 없고, 일부 필드는 출처 값에서 읽는다
PHARMACY = {
    "kind": "app",
    "id": "pharmacy-permits",
    "version": 1,
    "title": "우리 약국 약 허가 사항 변경 알림",
    "owner": "user:77",
    "sensitivity": "public",
    "sources": [{"id": "mfds_permits", "read": ["item_name", "change_text"]}],
    "engine": {
        "kind": "change_impact@1",
        "fields": {
            "item_name": {"type": "text", "from": {"meta": "item_name"}},
            "dosage": {
                "type": "text",
                "from": "extract",
                "guide": "용법·용량",
                "keywords": ["용법", "용량"],
            },
            "warnings": {
                "type": "list",
                "from": "extract",
                "guide": "사용상 주의사항 항목",
                "keywords": ["주의", "금기"],
            },
        },
    },
    "subjects": {"model": "watchlist"},
    "alerts": {
        "rules": [{"on": "field_changed", "fields": ["dosage", "warnings"], "to": "matched"}],
        "channels": ["inbox"],
        "hold": {"mass_impact": 200, "min_confidence": 0.7},
    },
    "budget": {"llm_usd_per_month": 2, "on_exceed": "pause_extraction"},
}


def app_of(data):
    app, problems = check_app(data)
    assert app is not None, problems
    return app


def segs(*texts):
    return match_segments([], segment_document(list(texts), source="body")).segments


class FakeGateway:
    def __init__(self, output):
        self.output, self.requests = output, []

    def complete(self, req):
        self.requests.append(req)
        return LLMResponse(output=self.output, input_tokens=1, output_tokens=1, cost_usd=0.0)


def test_the_generic_prompt_reproduces_the_grants_prompt_letter_for_letter():
    # 이미 녹화한 지원사업 앱의 요청이 바뀌지 않는다(녹화본 키가 같다)
    for t in (TEMPLATE_V1, TEMPLATE_V2):
        assert system_prompt(DOMAIN, t, list(ATTRIBUTES), NOTES) == _SYSTEM[t]
    assert dict(GRANTS_SPEC.system) == _SYSTEM and GRANTS_SPEC.heading == "공고 조각"
    assert spec_for(APP) == GRANTS_SPEC


def test_a_second_app_extracts_from_its_own_definition():
    app = app_of(PHARMACY)
    spec = spec_for(app)  # 지원사업 앱이 아니므로 앱 정의만으로
    assert set(spec.types) == {"dosage", "warnings"}  # 출처 값 필드는 LLM에 묻지 않는다
    assert spec.attributes is None and "자격 규칙" not in spec.system[TEMPLATE_V2]
    # 분야를 정하지 않으면 고정 문구(앱 제목을 쓰면 제목만 고쳐도 녹화본이 무효가 된다)
    assert spec.system[TEMPLATE_V2].startswith("너는 주어진 문서에서")

    seg = segs("용법·용량: 1회 1정, 1일 2회\n\n주의: 임부 금기")
    gw = FakeGateway(
        {"fields": [{"name": "dosage", "value_json": '"1회 1정, 1일 2회"', "sources": [1]}]}
    )
    out = SpecExtractor(gw, spec, template=TEMPLATE_V2)({"dosage", "warnings", "item_name"}, seg)
    req = gw.requests[0]
    assert "- dosage: 용법·용량" in req.input and "문서 조각:" in req.input
    assert req.output_schema["properties"]["fields"]["items"]["properties"]["name"]["enum"] == [
        "dosage",
        "warnings",
    ]
    assert out["dosage"].value == "1회 1정, 1일 2회" and out["dosage"].sources


def test_an_app_can_name_its_domain_and_heading():
    data = copy.deepcopy(PHARMACY)
    data["engine"]["prompt"] = {"domain": "의약품 허가 사항 변경 공고", "heading": "공고 조각"}
    spec = spec_for_app(app_of(data))
    assert spec.system[TEMPLATE_V1].startswith("너는 의약품 허가 사항 변경 공고에서")
    assert spec.heading == "공고 조각"


def test_a_grants_draft_with_a_new_guide_needs_a_new_extraction():
    data = APP.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data["version"] += 1
    data["engine"]["fields"]["amount"]["guide"] += " (부가세 제외)"
    draft = app_of(data)
    seg = segs("지원 규모: 기업당 최대 5천만원")
    old = build_request(spec_for(APP), {"amount"}, seg, template=TEMPLATE_V1)
    new = build_request(spec_for(draft), {"amount"}, seg, template=TEMPLATE_V1)
    assert request_key(old) != request_key(new)  # 녹화본이 없으니 미리 돌려 보기는 비용을 센다
    assert new.system == old.system  # 프롬프트는 같은 팩의 것


def test_notes_reach_apps_without_rules_and_rulesets_need_the_rules_model():
    data = copy.deepcopy(PHARMACY)
    data["engine"]["prompt"] = {"domain": "의약품 허가 사항", "notes": ["용량 단위는 mg로 적는다."]}
    spec = spec_for_app(app_of(data))
    assert "- 용량 단위는 mg로 적는다." in spec.system[TEMPLATE_V2]
    data["engine"]["fields"]["limits"] = {"type": "ruleset", "from": "extract", "guide": "조건"}
    app, problems = check_app(data)
    assert app is None and any("ruleset 필드는" in p.message for p in problems)


def test_draft_comparison_sees_prompt_and_attribute_changes():
    from noticedelta.backtest import same_extraction

    data = APP.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data["version"] += 1
    data["engine"]["prompt"] = {"domain": "지원사업 공고"}  # 필드 정의는 같고 프롬프트만
    draft = app_of(data)
    assert not same_extraction(APP, draft, spec_for)
    assert same_extraction(APP, APP, spec_for)
