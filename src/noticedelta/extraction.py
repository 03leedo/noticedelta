"""앱 정의로 만드는 LLM 필드 추출기 (설계 7.5, app-spec 2장). impact.Extractor 구현.

추출에 필요한 것은 모두 `ExtractSpec` 하나에 담는다: 필드 타입과 안내, 규칙 속성 사전, 틀별 시스템
프롬프트, 조각 머리말. 앱 정의만 있으면 `spec_for_app`이 일반 프롬프트로 만든다. 도메인 팩은 자기
프롬프트를 글자 그대로 줄 수 있다(지원사업 팩: llm.extract). 녹화본 키가 요청 글자에 걸리므로, 이미
녹화한 앱의 프롬프트는 바꾸지 않는다.

조각을 한 줄씩 넘기고, 값마다 근거 조각을 함께 받는다. 받은 값은 인용 검증을 통과해야 신뢰도 1.0을
유지한다. 틀(template)은 둘이다. v1은 `[lineage_id] 텍스트`, v2는 요청 안에서만 쓰는 짧은 번호
`[1] 텍스트`(빈 조각은 뺀다)이고, 답의 번호를 lineage_id로 되돌린다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from noticedelta.appspec import AppDefinition
from noticedelta.citation import verify_citation, verify_rule
from noticedelta.eligibility import Rule, RuleGroup
from noticedelta.impact import FieldValue
from noticedelta.llm.anthropic_provider import DEFAULT_MODEL
from noticedelta.llm.gateway import Gateway, LLMRequest
from noticedelta.segments import Segment
from noticedelta.values import coerce_shape, loads_lenient

TEMPLATE_V1 = "extract-v1"
TEMPLATE_V2 = "extract-v2"
TEMPLATE_VERSION = TEMPLATE_V2  # 새로 부르는 추출의 기본 틀
TEMPLATES = (TEMPLATE_V1, TEMPLATE_V2)
LOW_CONFIDENCE = 0.3
DEFAULT_HEADING = "문서 조각"
# 앱 제목을 분야로 쓰면 제목만 고쳐도 녹화본이 모두 무효가 된다. 분야는 engine.prompt.domain으로 정한다
DEFAULT_DOMAIN = "주어진 문서"

_LINE_FORMAT = {
    TEMPLATE_V1: "입력의 각 줄은 `[조각ID] 원문`이다. 요청된 필드만 뽑고, 값마다 근거가 된 조각ID를 sources에 적는다.",
    TEMPLATE_V2: "입력의 각 줄은 `[번호] 원문`이다. 요청된 필드만 뽑고, 값마다 근거가 된 줄의 번호를 sources에 적는다.",
}
_RULE_FORMAT = (
    '{"group": "all_of" | "any_of", "rules": [{"id": "r1", "attribute": 속성, "op": 연산자, "value": 값} 또는 하위 그룹],\n'
    '  "manual": [{"text": "속성 사전으로 표현할 수 없는 조건 원문"}]}'
)


def system_prompt(
    domain: str,
    template: str,
    attributes: Sequence[str] | None = None,
    notes: Sequence[str] = (),
) -> str:
    """일반 시스템 프롬프트. attributes가 있으면(규칙 필드가 있는 앱) 규칙 형식과 속성 사전을 붙인다."""
    lines = [
        f"너는 {domain}에서 정해진 필드만 뽑는 추출기다.",
        _LINE_FORMAT[template],
        "원문에 근거가 없는 필드는 출력하지 않는다. 추측하지 않는다.",
        "value_json에는 값을 JSON으로 직렬화한 문자열을 넣는다.",
    ]
    if attributes is None:  # 규칙 필드가 없는 앱: 참고 줄은 일반 지시 뒤에
        lines += [f"- {n}" for n in notes]
    else:
        lines += [
            "",
            "자격 규칙 형식:",
            _RULE_FORMAT,
            f"속성 사전: {', '.join(attributes)}",
            *(f"- {n}" for n in notes),
            "연산자: == != < <= > >= in not_in between",
            "사전에 없는 조건은 억지로 바꾸지 말고 manual에 원문 그대로 둔다.",
        ]
    return "\n".join(lines)


@dataclass(frozen=True)
class ExtractSpec:
    types: Mapping[str, str]  # 필드 → 값 타입(datetime, amount, rules, list, text)
    guides: Mapping[str, str]  # 필드 → LLM에 주는 안내
    system: Mapping[str, str]  # 틀 → 시스템 프롬프트
    attributes: Mapping[str, Any] | None = None  # 규칙 속성 사전(규칙 필드가 있을 때)
    heading: str = DEFAULT_HEADING  # 입력에서 조각 목록 앞의 머리말
    extras: dict = field(default_factory=dict)

    @property
    def rules_context(self) -> dict:
        return {"attributes": self.attributes or {}}


def spec_for_app(app: AppDefinition) -> ExtractSpec:
    """앱 정의만으로 만드는 추출 명세. 분야·머리말·참고 줄은 engine.prompt에서, 없으면 앱 제목으로."""
    prompt = app.engine.prompt
    domain = prompt.domain if prompt else DEFAULT_DOMAIN
    notes = prompt.notes if prompt else []
    heading = prompt.heading if prompt else DEFAULT_HEADING
    attributes = app.rule_attributes() if app.subjects.rules_field else None
    extracted = {n for n, f in app.engine.fields.items() if f.extracts}
    types = {n: t for n, t in app.value_types().items() if n in extracted}
    return ExtractSpec(
        types=types,
        guides={n: g for n, g in app.guides().items() if n in extracted},
        system={
            t: system_prompt(domain, t, list(attributes) if attributes else None, notes)
            for t in TEMPLATES
        },
        attributes=attributes,
        heading=heading,
    )


def _schema(fields: list[str], template: str = TEMPLATE_V1) -> dict:
    # v2의 근거는 정수 줄 번호. 문자열로 두면 학교 모델이 따옴표 없는 숫자를 쓰려다 형식에 막혀
    # 공백만 끝없이 내다 출력 한도에 걸렸다(2026-11-25, 작은 공고 하나로 재현).
    source = {"type": "integer"} if template == TEMPLATE_V2 else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": fields},
                        "value_json": {"type": "string"},
                        "sources": {"type": "array", "items": source},
                    },
                    "required": ["name", "value_json", "sources"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["fields"],
        "additionalProperties": False,
    }


def labeled(segments: Sequence[Segment], template: str = TEMPLATE_VERSION) -> dict[str, Segment]:
    """프롬프트의 줄 표시 → 조각. v1은 lineage_id, v2는 1부터 센 번호(빈 조각은 뺀다)."""
    if template == TEMPLATE_V1:
        return {s.lineage_id: s for s in segments}
    if template != TEMPLATE_V2:
        raise ValueError(f"모르는 추출 틀: {template}")
    kept = [s for s in segments if s.text.strip()]
    return {str(i): s for i, s in enumerate(kept, 1)}


def build_request(
    spec: ExtractSpec,
    fields: set[str],
    segments: Sequence[Segment],
    *,
    model: str = DEFAULT_MODEL,
    template: str = TEMPLATE_VERSION,
) -> LLMRequest:
    names = sorted(f for f in fields if f in spec.types)
    guide = "\n".join(f"- {n}: {spec.guides[n]}" for n in names)
    if template == TEMPLATE_V1:  # 녹화본 키가 그대로이도록 예전 그대로 만든다
        lines = "\n".join(f"[{s.lineage_id}] {s.text}" for s in segments)
    else:
        lines = "\n".join(f"[{k}] {s.text}" for k, s in labeled(segments, template).items())
    return LLMRequest(
        model=model,
        template_version=template,
        system=spec.system[template],
        input=f"추출할 필드:\n{guide}\n\n{spec.heading}:\n{lines}",
        output_schema=_schema(names, template),
    )


def _label(source) -> str:
    """답의 근거 표시를 줄 표시로. 모델이 "[3]"이나 숫자 3으로 주기도 한다."""
    return str(source).strip().strip("[]").strip()


def _iter_rules(group: RuleGroup):
    for node in group.rules:
        if isinstance(node, RuleGroup):
            yield from _iter_rules(node)
        else:
            yield node


class SpecExtractor:
    """ExtractSpec으로 필드를 뽑는다. 앱마다 하나씩 만든다."""

    def __init__(
        self,
        gateway: Gateway,
        spec: ExtractSpec,
        *,
        model: str = DEFAULT_MODEL,
        default_year: int | None = None,
        template: str = TEMPLATE_VERSION,
    ):
        self.gateway = gateway
        self.spec = spec
        self.model = model
        self.default_year = default_year
        self.template = template

    def __call__(self, fields: set[str], segments: Sequence[Segment]) -> dict[str, FieldValue]:
        wanted = {f for f in fields if f in self.spec.types}
        by_label = labeled(segments, self.template)
        if not wanted or not by_label:
            return {}
        req = build_request(self.spec, wanted, segments, model=self.model, template=self.template)
        resp = self.gateway.complete(req)

        out: dict[str, FieldValue] = {}
        for item in resp.output.get("fields", []):
            name = item.get("name")
            if name not in wanted or name in out:
                continue
            value = loads_lenient(item.get("value_json"))
            keys = item.get("sources", [])
            if self.template != TEMPLATE_V1:  # v1의 재생 결과가 바뀌지 않게 v1은 예전 그대로 맞춘다
                keys = map(_label, keys)
            hits = [by_label[k] for k in keys if k in by_label]
            sources = [h.lineage_id for h in hits]
            cited = [h.text for h in hits]
            fv = self._to_field(name, value, sources, cited)
            if fv is not None:
                out[name] = fv
        return out

    def _to_field(
        self, name: str, value, sources: list[str], cited: list[str]
    ) -> FieldValue | None:
        vtype = self.spec.types[name]
        value = coerce_shape(vtype, value)
        if value is None:
            return None
        if vtype == "rules":
            try:
                group = RuleGroup.model_validate(value, context=self.spec.rules_context)
            except ValidationError:
                return None
            for rule in _iter_rules(group):
                rule: Rule
                if not verify_rule(rule, cited, self.spec.attributes or {}):
                    rule.confidence = min(rule.confidence, LOW_CONFIDENCE)
            # 규칙이 하나도 없으면 믿지 않는다. 판정기도 빈 그룹을 확인 필요로 본다.
            confs = [r.confidence for r in _iter_rules(group)] or [LOW_CONFIDENCE]
            return FieldValue(name, vtype, group.model_dump(), sources, min(confs))
        ok = bool(cited) and verify_citation(vtype, value, cited, default_year=self.default_year)
        return FieldValue(name, vtype, value, sources, 1.0 if ok else LOW_CONFIDENCE)
