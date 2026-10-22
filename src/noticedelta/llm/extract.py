"""LLM 필드 추출기 (설계 7.5). impact.Extractor 구현.

조각을 `[lineage_id] 텍스트` 줄로 넘기고, 값마다 근거 조각을 함께 받는다.
받은 값은 인용 검증을 통과해야 신뢰도 1.0을 유지한다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import ValidationError

from noticedelta.citation import verify_citation, verify_rule
from noticedelta.eligibility import ATTRIBUTES, Rule, RuleGroup
from noticedelta.impact import FieldValue
from noticedelta.llm.anthropic_provider import DEFAULT_MODEL
from noticedelta.llm.gateway import Gateway, LLMRequest
from noticedelta.segments import Segment

TEMPLATE_VERSION = "extract-v1"
LOW_CONFIDENCE = 0.3

FIELD_TYPES: dict[str, str] = {
    "deadline": "datetime",
    "amount": "amount",
    "eligibility_rules": "rules",
    "required_documents": "list",
    "contact": "text",
}

FIELD_GUIDE: dict[str, str] = {
    "deadline": '신청 마감 일시. "YYYY-MM-DD HH:MM" 문자열 (시각이 없으면 "YYYY-MM-DD")',
    "amount": "기업당 최대 지원 금액. 원 단위 정수",
    "eligibility_rules": "신청 자격 규칙. 아래 규칙 형식의 JSON 객체",
    "required_documents": "제출 서류 이름 목록. 문자열 배열",
    "contact": "문의처. 문자열",
}

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


def _schema(fields: list[str]) -> dict:
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
                        "sources": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "value_json", "sources"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["fields"],
        "additionalProperties": False,
    }


def build_request(
    fields: set[str], segments: Sequence[Segment], *, model: str = DEFAULT_MODEL
) -> LLMRequest:
    names = sorted(f for f in fields if f in FIELD_TYPES)
    guide = "\n".join(f"- {n}: {FIELD_GUIDE[n]}" for n in names)
    lines = "\n".join(f"[{s.lineage_id}] {s.text}" for s in segments)
    return LLMRequest(
        model=model,
        template_version=TEMPLATE_VERSION,
        system=SYSTEM_PROMPT,
        input=f"추출할 필드:\n{guide}\n\n공고 조각:\n{lines}",
        output_schema=_schema(names),
    )


def _iter_rules(group: RuleGroup):
    for node in group.rules:
        if isinstance(node, RuleGroup):
            yield from _iter_rules(node)
        else:
            yield node


class LLMExtractor:
    def __init__(
        self, gateway: Gateway, *, model: str = DEFAULT_MODEL, default_year: int | None = None
    ):
        self.gateway = gateway
        self.model = model
        self.default_year = default_year

    def __call__(self, fields: set[str], segments: Sequence[Segment]) -> dict[str, FieldValue]:
        wanted = {f for f in fields if f in FIELD_TYPES}
        if not wanted or not segments:
            return {}
        resp = self.gateway.complete(build_request(wanted, segments, model=self.model))
        text_by_id = {s.lineage_id: s.text for s in segments}

        out: dict[str, FieldValue] = {}
        for item in resp.output.get("fields", []):
            name = item.get("name")
            if name not in wanted or name in out:
                continue
            try:
                value = json.loads(item["value_json"])
            except (KeyError, json.JSONDecodeError):
                continue
            sources = [s for s in item.get("sources", []) if s in text_by_id]
            cited = [text_by_id[s] for s in sources]
            fv = self._to_field(name, value, sources, cited)
            if fv is not None:
                out[name] = fv
        return out

    def _to_field(
        self, name: str, value, sources: list[str], cited: list[str]
    ) -> FieldValue | None:
        vtype = FIELD_TYPES[name]
        if vtype == "rules":
            try:
                group = RuleGroup.model_validate(value)
            except ValidationError:
                return None
            for rule in _iter_rules(group):
                rule: Rule
                if not verify_rule(rule, cited):
                    rule.confidence = min(rule.confidence, LOW_CONFIDENCE)
            # 규칙이 하나도 없으면 믿지 않는다. 판정기도 빈 그룹을 확인 필요로 본다.
            confs = [r.confidence for r in _iter_rules(group)] or [LOW_CONFIDENCE]
            return FieldValue(name, vtype, group.model_dump(), sources, min(confs))
        ok = bool(cited) and verify_citation(vtype, value, cited, default_year=self.default_year)
        return FieldValue(name, vtype, value, sources, 1.0 if ok else LOW_CONFIDENCE)
