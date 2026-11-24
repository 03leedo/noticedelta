"""비교할 방식들 (DESIGN 12.2). 모두 같은 이벤트를 받아 "바뀌었다고 본 필드"를 낸다.

    B0Meta   API의 구조화 필드만 코드로 비교 (LLM 0회)
    B2Full   새 버전 전체를 다시 추출해 이전 값과 비교
    B3Diff   바뀐 조각(이전 → 이후)과 이전 값을 LLM에 주고 무엇이 바뀌었는지 묻는다
    Engine   변경 영향 엔진: 영향받은 필드만, 바뀐 조각과 기존 근거만 다시 추출

어떤 필드를 보는지(이름·타입·설명·지시문)는 도메인 팩이 Fields로 넘긴다.
처음 상태(첫 버전의 전체 추출)는 LLM을 쓰는 세 방식이 똑같이 들이므로 이벤트 비용에 넣지 않는다.
녹화본이 없어 판단하지 못한 이벤트는 changed=None으로 내고, 지표에서 뺀다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from noticedelta.evaluation.dataset import EvalEvent
from noticedelta.impact import Extractor, FieldValue, keyword_detector, run_impact
from noticedelta.llm.anthropic_provider import DEFAULT_MODEL
from noticedelta.llm.gateway import Gateway, LLMRequest, MissingCassette
from noticedelta.llm.openai_compat import ProviderError
from noticedelta.segments import Segment
from noticedelta.values import coerce_shape, values_equal

NO_CASSETTE = "녹화본 없음"
CALL_FAILED = "호출 실패"  # 공급자가 끝내 답하지 못함(시간 초과, 한도). 그 사업은 거기까지만 잰다


@dataclass
class Outcome:
    changed: set[str] | None  # None: 이 방식으로는 판단하지 못했다
    calls: int = 0  # LLM 호출 수
    input_chars: int = 0  # LLM에 넘긴 조각 글자 수
    note: str = ""


@dataclass(frozen=True)
class Fields:
    """평가할 필드의 정의. 도메인 팩이 넘긴다."""

    types: Mapping[str, str]  # 필드 이름 → 값 타입 (values.ValueType)
    guide: Mapping[str, str] = field(default_factory=dict)  # LLM에 보여 줄 필드 설명 (B3)
    meta: Sequence[str] = ()  # 출처 API가 구조화해 주는 필드 (B0가 비교한다)
    diff_system: str = ""  # B3의 지시문
    keywords: Mapping[str, str] = field(default_factory=dict)  # 필드별 낱말 (엔진의 후보 찾기)


class Method(Protocol):
    def start(self, segments: Sequence[Segment]) -> None: ...
    def step(self, ev: EvalEvent) -> Outcome: ...


def diff_fields(old: Mapping[str, FieldValue], new: Mapping[str, FieldValue]) -> set[str]:
    """타입별 비교로 값이 달라진 필드. 생기거나 사라진 필드도 바뀐 것으로 본다."""
    out = set()
    for name in set(old) | set(new):
        a, b = old.get(name), new.get(name)
        if a is None or b is None or not values_equal(b.value_type, a.value, b.value):
            out.add(name)
    return out


class B0Meta:
    def __init__(self, spec: Fields):
        self.spec = spec

    def start(self, segments: Sequence[Segment]) -> None:
        return None

    def step(self, ev: EvalEvent) -> Outcome:
        return Outcome({f for f in self.spec.meta if ev.meta_before.get(f) != ev.meta_after.get(f)})


class _Meter:
    """추출기를 감싸 호출 수와 넘긴 글자 수를 센다."""

    def __init__(self, extractor: Extractor):
        self.extractor = extractor
        self.calls = self.chars = 0

    def __call__(self, fields: set[str], segments: Sequence[Segment]) -> dict[str, FieldValue]:
        self.calls += 1
        self.chars += sum(len(s.text) for s in segments)
        return self.extractor(fields, segments)

    def take(self) -> tuple[int, int]:
        out = (self.calls, self.chars)
        self.calls = self.chars = 0
        return out


class _WithState:
    def __init__(self, extractor: Extractor, spec: Fields):
        self.spec = spec
        self.meter = _Meter(extractor)
        self.fields: dict[str, FieldValue] | None = None  # None: 처음 상태를 만들지 못했다

    def start(self, segments: Sequence[Segment]) -> None:
        try:
            self.fields = self.meter(set(self.spec.types), segments) if segments else {}
        except (MissingCassette, ProviderError):
            self.fields = None
        self.meter.take()  # 처음 추출은 이벤트 비용이 아니다

    def step(self, ev: EvalEvent) -> Outcome:
        if self.fields is None:
            return Outcome(None, note=NO_CASSETTE)
        try:
            changed = self._step(ev)
        except (MissingCassette, ProviderError) as e:
            # 여기서 끊기면 뒤 이벤트의 이전 값도 믿을 수 없다. 이 사업은 여기까지만 잰다.
            self.fields = None
            self.meter.take()
            return Outcome(
                None, note=NO_CASSETTE if isinstance(e, MissingCassette) else CALL_FAILED
            )
        calls, chars = self.meter.take()
        return Outcome(changed, calls, chars)

    def _step(self, ev: EvalEvent) -> set[str]:
        raise NotImplementedError


class B2Full(_WithState):
    def _step(self, ev: EvalEvent) -> set[str]:
        new = self.meter(set(self.spec.types), ev.change.segments)
        changed = diff_fields(self.fields, new)
        self.fields = new
        return changed


class Engine(_WithState):
    def _step(self, ev: EvalEvent) -> set[str]:
        res = run_impact(
            item_id=ev.item_id,
            to_version_id=ev.event_id,
            change=ev.change,
            old_fields=self.fields,
            extractor=self.meter,
            candidate_detector=keyword_detector(self.spec.keywords, self.spec.types),
            subscriptions=[],
            profiles={},
            old_judgments={},
            old_segments=ev.old_segments,
            unknown_lineages=ev.unknown,
        )
        self.fields = res.fields
        return set(res.real_changes)


DIFF_TEMPLATE = "diff-v1"


def _diff_schema(names: Sequence[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": list(names)},
                        "changed": {"type": "boolean"},
                        "value_json": {"type": "string"},
                    },
                    "required": ["name", "changed", "value_json"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["fields"],
        "additionalProperties": False,
    }


def build_diff_request(
    fields: Mapping[str, FieldValue], ev: EvalEvent, spec: Fields, *, model: str = DEFAULT_MODEL
) -> LLMRequest:
    old = {s.lineage_id: s for s in ev.old_segments}
    new = {s.lineage_id: s for s in ev.change.segments}
    lines = []
    for lid in ev.change.modified:
        lines += [
            f"[{lid}] 이전: {old[lid].text if lid in old else ''}",
            f"[{lid}] 이후: {new[lid].text}",
        ]
    lines += [f"[{lid}] 추가: {new[lid].text}" for lid in ev.change.added]
    lines += [f"[{lid}] 삭제: {old[lid].text}" for lid in ev.change.removed if lid in old]
    before = "\n".join(
        f"- {name} ({spec.guide.get(name, name)}): {json.dumps(fv.value, ensure_ascii=False)}"
        for name, fv in sorted(fields.items())
    )
    return LLMRequest(
        model=model,
        template_version=DIFF_TEMPLATE,
        system=spec.diff_system,
        input=f"정정 전 필드 값:\n{before}\n\n바뀐 조각:\n" + "\n".join(lines),
        output_schema=_diff_schema(sorted(spec.types)),
    )


class B3Diff(_WithState):
    """바뀌었는지는 LLM의 답을 그대로 믿는다(타입별 비교를 하지 않는 것이 엔진과의 차이다)."""

    def __init__(
        self, extractor: Extractor, gateway: Gateway, spec: Fields, *, model: str = DEFAULT_MODEL
    ):
        super().__init__(extractor, spec)
        self.gateway, self.model = gateway, model

    def _step(self, ev: EvalEvent) -> set[str]:
        req = build_diff_request(self.fields, ev, self.spec, model=self.model)
        self.meter.calls += 1
        self.meter.chars += len(req.input)
        resp = self.gateway.complete(req)
        changed = set()
        for item in resp.output.get("fields", []):
            name = item.get("name")
            if name not in self.spec.types or not item.get("changed"):
                continue
            changed.add(name)
            try:
                value = json.loads(item["value_json"])
            except (KeyError, json.JSONDecodeError):
                continue
            value = coerce_shape(self.spec.types[name], value)
            if value is None:
                self.fields.pop(name, None)  # 바뀌었는데 값이 없다 = 사라졌다
                continue
            self.fields[name] = FieldValue(name, self.spec.types[name], value, sources=[])
        return changed
