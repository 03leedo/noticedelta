"""앱 정의 규격(appspec)과 지원사업 앱 정의 한 장(apps/grant-alerts.json)."""

import copy
import json

import pytest

from noticedelta import grants
from noticedelta.appspec import AppDefinition, AppSpecError, check_app, load_app
from noticedelta.eligibility.rules import Attribute, Derive
from noticedelta.sources import CATALOG

BASE = json.loads(grants.DEFINITION.read_text(encoding="utf-8"))


def problems(change, *, sources=None):
    data = copy.deepcopy(BASE)
    change(data)
    app, found = check_app(data, sources=sources)
    assert app is None
    return [str(p) for p in found]


def test_grant_alerts_definition_is_valid():
    app = load_app(grants.DEFINITION)
    assert (app.id, app.version, app.sensitivity) == ("grant-alerts", 1, "personal")
    assert len(app.content_hash()) == 64
    assert app.meta_fields() == ("deadline",)


# 정의로 옮기기 전(2026-11-24) grants.py에 있던 값. LLM 안내와 속성 사전이 바뀌면 녹화본 키가 바뀌어
# 지난 실측을 다시 돌려야 하므로, 옮기면서 한 글자도 바뀌지 않았는지 고정한다.
OLD_GUIDE = {
    "deadline": '신청 마감 일시. "YYYY-MM-DD HH:MM" 문자열 (시각이 없으면 "YYYY-MM-DD")',
    "amount": "기업당 최대 지원 금액. 원 단위 정수",
    "eligibility_rules": "신청 자격 규칙. 아래 규칙 형식의 JSON 객체",
    "required_documents": "제출 서류 이름 목록. 문자열 배열",
    "contact": "문의처. 문자열",
}
OLD_KEYWORDS = {
    "deadline": r"마감|접수\s*기간|신청\s*기간|모집\s*기간|소진\s*시\s*까지",
    "amount": r"지원\s*금|지원\s*규모|지원\s*한도|사업비|최대\s*\d",
    "eligibility_rules": (
        r"신청\s*자격|지원\s*대상|자격\s*요건|제외\s*대상|업력|창업\s*\d+\s*년|만\s*\d+\s*세|"
        r"연령|나이|소재|예비\s*창업|재창업|여성|매출|상시\s*근로자|중복\s*수혜"
    ),
    "required_documents": (
        r"(?:제출|구비|증빙|첨부|신청|필요|준비)\s*서류|서류\s*(?:제출|목록)|증빙\s*자료|사본"
    ),
    "contact": r"문의|연락처|담당자|☎|\d{2,3}-\d{3,4}-\d{4}",
}
SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북",
        "충남", "전북", "전남", "경북", "경남", "제주")  # fmt: skip
OLD_ATTRIBUTES = {
    "is_pre_founder": Attribute(bool),
    "business_age_months": Attribute(
        int, units=("년", "개월"), scale=12, derive=Derive("months_since", "founded_on")
    ),
    "ceo_age": Attribute(int, units=("세",), derive=Derive("age", "ceo_birth_date")),
    "region_sido": Attribute(str, values=SIDO),
    "industry_ksic": Attribute(str),
    "business_type": Attribute(str, values=("individual", "corporation")),
    "revenue_krw": Attribute(int, money=True),
    "employees": Attribute(int, units=("명", "인")),
    "is_women_owned": Attribute(bool),
    "is_restart": Attribute(bool),
}


def test_moving_to_data_changed_nothing_the_engine_sees():
    assert grants.FIELD_TYPES == {
        "deadline": "datetime",
        "amount": "amount",
        "eligibility_rules": "rules",
        "required_documents": "list",
        "contact": "text",
    }
    assert grants.FIELD_GUIDE == OLD_GUIDE
    assert grants.KEYWORDS == OLD_KEYWORDS
    assert grants.ATTRIBUTES == OLD_ATTRIBUTES
    assert list(grants.ATTRIBUTES) == list(OLD_ATTRIBUTES)  # 프롬프트에 이 순서로 들어간다
    assert grants.SIDO == SIDO
    assert grants.RULES_FIELD == "eligibility_rules"
    assert (grants.MASS_IMPACT, grants.MIN_CONFIDENCE) == (20, 0.7)


def adapter_keys():
    """출처 카탈로그가 앱에 읽게 해 주는 키."""
    return {sid: sheet.readable_keys() for sid, sheet in CATALOG.items()}


def test_sources_read_only_catalog_keys():
    app = load_app(grants.DEFINITION, sources=adapter_keys())
    for s in app.sources:
        assert set(s.read) <= adapter_keys()[s.id]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda d: d["alerts"]["rules"][1]["fields"].append("amount2"),
            "alerts.rules.1.fields: 없는 필드: amount2 (정의된 필드: amount, contact,",
        ),
        (
            lambda d: d["subjects"].update(rules_field="contact"),
            "subjects.rules_field: contact는 ruleset이 아니다",
        ),
        (
            lambda d: d["subjects"]["attributes"]["ceo_age"].update(derive="years(ceo_birth_date)"),
            "없는 파생 함수 years (있는 함수: age, months_since)",
        ),
        (
            lambda d: d["subjects"]["attributes"]["ceo_age"].update(derive="age(region_sido)"),
            "region_sido는 date 속성이 아니다",
        ),
        (
            lambda d: d["engine"]["fields"]["amount"]["keywords"].append("최대("),
            "engine.fields.amount.keywords.5: 정규식이 아니다",
        ),
        (
            lambda d: d["engine"]["fields"]["amount"].pop("guide"),
            "engine.fields.amount.guide: 글에서 뽑는 필드에 안내가 없다",
        ),
        (
            lambda d: d["alerts"]["channels"].append("webhook"),
            "밖의 주소로 보내는 채널은 승인 기록이 있어야 한다",
        ),
        (lambda d: d.update(sensitivity="sensitive"), "sensitive 앱은 외부 모델로 보내지 않는다"),
        (
            lambda d: d["alerts"]["rules"][0]["urgent"].append("eligible→maybe"),
            "판정 변화가 아니다: eligible→maybe",
        ),
        (
            lambda d: d["labels"]["fields"].update(nope="없음"),
            "labels.fields: 없는 필드의 이름: nope",
        ),
    ],
)
def test_problems_say_how_to_fix_them(change, expected):
    found = problems(change)
    assert any(expected in p for p in found), found


def test_shape_errors_name_the_path_and_the_fix():
    found = problems(lambda d: d["engine"]["fields"]["amount"].update(typ="amount"))
    assert any("engine.fields.amount.typ" in p and "규격에 없는 칸" in p for p in found), found
    found = problems(lambda d: d["engine"]["fields"]["amount"].update(type="money"))
    assert any("engine.fields.amount.type" in p and "고를 수 있는 값" in p for p in found), found
    found = problems(lambda d: d.pop("budget"))
    assert any(p.startswith("budget:") and "꼭 있어야" in p for p in found), found


def test_unknown_source_and_keys_are_reported_with_choices():
    keys = adapter_keys()
    found = problems(lambda d: d["sources"][0].update(id="g2b"), sources=keys)
    assert any("카탈로그에 없는 출처 g2b (있는 출처: bizinfo, kstartup)" in p for p in found)
    found = problems(lambda d: d["sources"][1]["read"].append("biz_title"), sources=keys)
    assert any("kstartup가 내지 않는 키: biz_title" in p for p in found)


def test_load_app_raises_with_all_problems(tmp_path):
    data = copy.deepcopy(BASE)
    data["subjects"]["rules_field"] = "nope"
    data["alerts"]["channels"] = ["webhook"]
    path = tmp_path / "app.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AppSpecError) as e:
        load_app(path)
    assert len(e.value.problems) == 2


def test_json_schema_is_published_for_agents():
    schema = AppDefinition.model_json_schema(by_alias=True)
    assert {"engine", "subjects", "alerts", "budget"} <= set(schema["properties"])
    field = schema["$defs"]["FieldSpec"]
    assert "from" in field["properties"] and "from_" not in field["properties"]


def test_content_hash_follows_content_not_formatting():
    a = AppDefinition.model_validate(BASE)
    b = AppDefinition.model_validate(json.loads(json.dumps(BASE, indent=None)))
    assert a.content_hash() == b.content_hash()
    changed = copy.deepcopy(BASE)
    changed["engine"]["fields"]["amount"]["guide"] += "."
    assert AppDefinition.model_validate(changed).content_hash() != a.content_hash()


def test_published_schema_file_is_current():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs" / "app-schema.json"
    fresh = AppDefinition.model_json_schema(by_alias=True)
    # 규격을 고치면 다시 쓴다: uv run python -c "...model_json_schema..." (docs/app-spec.md 3장)
    assert json.loads(path.read_text(encoding="utf-8")) == fresh
