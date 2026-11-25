"""지원사업 팩의 필드 추출(설계 7.5): 플랫폼 추출기(extraction)에 이 앱의 프롬프트를 준다.

추출기 본체는 extraction.py(엔진 층)에 있다. 이 모듈은 지원사업 앱의 ExtractSpec을 만들고, 예전 이름
(LLMExtractor, build_request, TEMPLATE_V1 …)을 그대로 내보낸다.

녹화본 키가 요청 글자에 걸리므로 v1의 요청은 한 글자도 바꾸지 않는다. 그래서 SYSTEM_PROMPT를 글자
그대로 두고, 일반 생성기(extraction.system_prompt)가 같은 글자를 내는지 불러올 때 확인한다.
- extract-v1: `[lineage_id] 텍스트`. 12자리 ID가 입력 토큰의 절반 가까이를 먹었다(녹화본 60개 분석).
- extract-v2(기본): 요청 안에서만 쓰는 짧은 번호 `[1] 텍스트`, 빈 조각은 뺀다.
"""

from __future__ import annotations

from collections.abc import Sequence

from noticedelta.appspec import AppDefinition
from noticedelta.extraction import (
    LOW_CONFIDENCE,
    TEMPLATE_V1,
    TEMPLATE_V2,
    TEMPLATE_VERSION,
    TEMPLATES,
    ExtractSpec,
    SpecExtractor,
    _label,
    _schema,
    labeled,
    spec_for_app,
    system_prompt,
)
from noticedelta.extraction import build_request as _build_request
from noticedelta.grants import APP, ATTRIBUTES, FIELD_GUIDE, FIELD_TYPES
from noticedelta.llm.anthropic_provider import DEFAULT_MODEL
from noticedelta.llm.gateway import Gateway, LLMRequest
from noticedelta.segments import Segment

__all__ = [
    "DIFF_SYSTEM",
    "FIELD_GUIDE",
    "FIELD_TYPES",
    "LOW_CONFIDENCE",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_V2",
    "TEMPLATE_V1",
    "TEMPLATE_V2",
    "TEMPLATE_VERSION",
    "GRANTS_SPEC",
    "LLMExtractor",
    "build_request",
    "grants_spec",
    "labeled",
    "spec_for",
    "_label",
    "_schema",
]

DOMAIN = "한국 정부 지원사업 공고"
HEADING = "공고 조각"
NOTES = [
    "business_age_months는 개월 수(7년 이내 → <= 84), ceo_age는 만 나이, revenue_krw는 원 단위."
]

SYSTEM_PROMPT = f"""너는 한국 정부 지원사업 공고에서 정해진 필드만 뽑는 추출기다.
입력의 각 줄은 `[조각ID] 원문`이다. 요청된 필드만 뽑고, 값마다 근거가 된 조각ID를 sources에 적는다.
원문에 근거가 없는 필드는 출력하지 않는다. 추측하지 않는다.
value_json에는 값을 JSON으로 직렬화한 문자열을 넣는다.

자격 규칙 형식:
{{"group": "all_of" | "any_of", "rules": [{{"id": "r1", "attribute": 속성, "op": 연산자, "value": 값}} 또는 하위 그룹],
  "manual": [{{"text": "속성 사전으로 표현할 수 없는 조건 원문"}}]}}
속성 사전: {", ".join(ATTRIBUTES)}
- business_age_months는 개월 수(7년 이내 → <= 84), ceo_age는 만 나이, revenue_krw는 원 단위.
연산자: == != < <= > >= in not_in between
사전에 없는 조건은 억지로 바꾸지 말고 manual에 원문 그대로 둔다."""

# v2는 첫 두 줄만 다르다: 조각ID 대신 줄 번호를 적게 한다.
SYSTEM_PROMPT_V2 = SYSTEM_PROMPT.replace(
    "입력의 각 줄은 `[조각ID] 원문`이다. 요청된 필드만 뽑고, 값마다 근거가 된 조각ID를 sources에 적는다.",
    "입력의 각 줄은 `[번호] 원문`이다. 요청된 필드만 뽑고, 값마다 근거가 된 줄의 번호를 sources에 적는다.",
)
_SYSTEM = {TEMPLATE_V1: SYSTEM_PROMPT, TEMPLATE_V2: SYSTEM_PROMPT_V2}
# 일반 생성기가 이 앱의 프롬프트를 글자 그대로 내야 녹화본을 그대로 쓴다
assert all(system_prompt(DOMAIN, t, list(ATTRIBUTES), NOTES) == _SYSTEM[t] for t in TEMPLATES)


DIFF_SYSTEM = """너는 한국 정부 지원사업 정정공고에서 무엇이 바뀌었는지 판단한다.
입력에는 정정 전 필드 값과, 바뀐 조각의 이전 글과 이후 글이 있다.
필드마다 값이 실제로 바뀌었는지 판단하고, 바뀌었으면 새 값을 value_json에 JSON 문자열로 넣는다.
문구만 바뀌고 값이 같으면 changed는 false다. 조각에 근거가 없는 필드는 출력하지 않는다."""


def grants_spec(app: AppDefinition = APP) -> ExtractSpec:
    """지원사업 앱(또는 그 초안)의 추출 명세: 이 팩의 분야·머리말·참고 줄에 그 판의 필드와 속성."""
    base = spec_for_app(app)
    attributes = base.attributes
    system = {
        t: system_prompt(DOMAIN, t, list(attributes) if attributes else None, NOTES)
        for t in TEMPLATES
    }
    return ExtractSpec(base.types, base.guides, system, attributes, HEADING)


GRANTS_SPEC = grants_spec()


def spec_for(app: AppDefinition) -> ExtractSpec:
    """앱의 추출 명세. 지원사업 앱은 이 팩의 프롬프트로, 다른 앱은 앱 정의만으로 만든다."""
    return grants_spec(app) if app.id == APP.id and app.engine.prompt is None else spec_for_app(app)


def build_request(
    fields: set[str],
    segments: Sequence[Segment],
    *,
    model: str = DEFAULT_MODEL,
    template: str = TEMPLATE_VERSION,
    spec: ExtractSpec = GRANTS_SPEC,
) -> LLMRequest:
    return _build_request(spec, fields, segments, model=model, template=template)


class LLMExtractor(SpecExtractor):
    """지원사업 앱의 추출기(예전 이름). spec을 주면 다른 앱·초안에도 쓴다."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        model: str = DEFAULT_MODEL,
        default_year: int | None = None,
        template: str = TEMPLATE_VERSION,
        spec: ExtractSpec = GRANTS_SPEC,
    ):
        super().__init__(gateway, spec, model=model, default_year=default_year, template=template)
