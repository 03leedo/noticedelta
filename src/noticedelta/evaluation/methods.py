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
from noticedelta.llm.gateway import BudgetExceeded, Gateway, LLMRequest, MissingCassette
from noticedelta.llm.openai_compat import ProviderError
from noticedelta.segments import Segment
from noticedelta.values import coerce_shape, loads_lenient, values_equal

NO_CASSETTE = "녹화본 없음"
CALL_FAILED = "호출 실패"  # 공급자가 끝내 답하지 못함(시간 초과, 한도). 그 사업은 거기까지만 잰다
OVER_BUDGET = "예산 초과"  # 이번 실행의 토큰 예산을 넘어 보내지 않았다
_STOPS = (MissingCassette, ProviderError, BudgetExceeded)


def _note(e: Exception) -> str:
    if isinstance(e, MissingCassette):
        return NO_CASSETTE
    return OVER_BUDGET if isinstance(e, BudgetExceeded) else CALL_FAILED


@dataclass
class Outcome:
    changed: set[str] | None  # None: 이 방식으로는 판단하지 못했다
    calls: int = 0  # LLM 호출 수
    input_chars: int = 0  # LLM에 넘긴 조각 글자 수
    note: str = ""
    input_tokens: int = 0  # 공급자가 센 토큰(녹화본이면 녹화 때 값)
    output_tokens: int = 0
    planned_calls: int = 0  # 미리 세기(PLAN): 녹화본이 없어 보냈어야 할 호출
    planned_tokens: int = 0  # 그 호출들의 입력 토큰 어림


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


_TOKEN_KEYS = ("input_tokens", "output_tokens", "planned_calls", "planned_tokens")


class _Meter:
    """추출기를 감싸 호출 수와 넘긴 글자 수를 세고, 게이트웨이의 토큰 합계에서 이번 몫을 뗀다."""

    def __init__(self, extractor: Extractor):
        self.extractor = extractor
        self.calls = self.chars = 0
        self.gateways: list[Gateway] = []
        if isinstance(gw := getattr(extractor, "gateway", None), Gateway):
            self.watch(gw)

    def watch(self, gateway: Gateway) -> None:
        if all(g is not gateway for g in self.gateways):
            self.gateways.append(gateway)
            self._mark = self._totals()

    def _totals(self) -> dict[str, int]:
        return {k: sum(getattr(g.usage, k) for g in self.gateways) for k in _TOKEN_KEYS}

    def __call__(self, fields: set[str], segments: Sequence[Segment]) -> dict[str, FieldValue]:
        self.calls += 1
        self.chars += sum(len(s.text) for s in segments)
        return self.extractor(fields, segments)

    def take(self) -> Outcome:
        now = self._totals() if self.gateways else dict.fromkeys(_TOKEN_KEYS, 0)
        mark = getattr(self, "_mark", now)
        out = Outcome(None, self.calls, self.chars, **{k: now[k] - mark[k] for k in _TOKEN_KEYS})
        self.calls = self.chars = 0
        self._mark = now
        return out


class _WithState:
    def __init__(self, extractor: Extractor, spec: Fields):
        self.spec = spec
        self.meter = _Meter(extractor)
        self.fields: dict[str, FieldValue] | None = None  # None: 처음 상태를 만들지 못했다
        self.stopped = NO_CASSETTE  # 끊긴 이유. 그 뒤 이벤트가 이어받는다

    def start(self, segments: Sequence[Segment]) -> None:
        try:
            self.fields = self.meter(set(self.spec.types), segments) if segments else {}
        except _STOPS as e:
            self.fields = None
            self.stopped = _note(e)
        self.start_cost = (
            self.meter.take()
        )  # 처음 추출은 이벤트 비용이 아니다(미리 세기에는 넣는다)

    def step(self, ev: EvalEvent) -> Outcome:
        if self.fields is None:
            return Outcome(None, note=self.stopped)
        try:
            changed = self._step(ev)
        except _STOPS as e:
            # 여기서 끊기면 뒤 이벤트의 이전 값도 믿을 수 없다. 이 사업은 여기까지만 잰다.
            self.fields = None
            self.stopped = _note(e)
            out = self.meter.take()
            if not isinstance(e, ProviderError):
                out = Outcome(None)  # 보내지 않았다(녹화본 없음, 예산 초과): 호출로 세지 않는다
            # 보냈다가 실패했으면 이 정정에서 쓴 호출·글자·앞 호출의 토큰을 남긴다(판단은 못 함).
            # 실패한 호출 자체의 토큰은 게이트웨이의 예산·live_tokens에만 들어간다
            out.note = self.stopped
            return out
        out = self.meter.take()
        out.changed = changed
        return out

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
DIFF_TEMPLATE_V2 = "diff-v2"  # 추출 v2와 같은 절약: 조각 ID 대신 요청 안의 짧은 번호


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
    fields: Mapping[str, FieldValue],
    ev: EvalEvent,
    spec: Fields,
    *,
    model: str = DEFAULT_MODEL,
    template: str = DIFF_TEMPLATE,
) -> LLMRequest:
    old = {s.lineage_id: s for s in ev.old_segments}
    new = {s.lineage_id: s for s in ev.change.segments}
    shown: dict[str, str] = {}  # lineage_id → 줄 표시. v1은 ID 그대로, v2는 1부터 센 번호

    def tag(lid: str) -> str:
        if template == DIFF_TEMPLATE:
            return lid
        return shown.setdefault(lid, str(len(shown) + 1))

    lines = []
    for lid in ev.change.modified:
        lines += [
            f"[{tag(lid)}] 이전: {old[lid].text if lid in old else ''}",
            f"[{tag(lid)}] 이후: {new[lid].text}",
        ]
    lines += [f"[{tag(lid)}] 추가: {new[lid].text}" for lid in ev.change.added]
    lines += [f"[{tag(lid)}] 삭제: {old[lid].text}" for lid in ev.change.removed if lid in old]
    before = "\n".join(
        f"- {name} ({spec.guide.get(name, name)}): {json.dumps(fv.value, ensure_ascii=False)}"
        for name, fv in sorted(fields.items())
    )
    return LLMRequest(
        model=model,
        template_version=template,
        system=spec.diff_system,
        input=f"정정 전 필드 값:\n{before}\n\n바뀐 조각:\n" + "\n".join(lines),
        output_schema=_diff_schema(sorted(spec.types)),
    )


class B3Diff(_WithState):
    """바뀌었는지는 LLM의 답을 그대로 믿는다(타입별 비교를 하지 않는 것이 엔진과의 차이다)."""

    def __init__(
        self,
        extractor: Extractor,
        gateway: Gateway,
        spec: Fields,
        *,
        model: str = DEFAULT_MODEL,
        template: str = DIFF_TEMPLATE,
    ):
        super().__init__(extractor, spec)
        self.gateway, self.model, self.template = gateway, model, template
        self.meter.watch(gateway)

    def _step(self, ev: EvalEvent) -> set[str]:
        req = build_diff_request(
            self.fields, ev, self.spec, model=self.model, template=self.template
        )
        self.meter.calls += 1
        self.meter.chars += len(req.input)
        resp = self.gateway.complete(req)
        changed = set()
        for item in resp.output.get("fields", []):
            name = item.get("name")
            if name not in self.spec.types or not item.get("changed"):
                continue
            changed.add(name)
            value = coerce_shape(self.spec.types[name], loads_lenient(item.get("value_json")))
            if value is None:
                self.fields.pop(name, None)  # 바뀌었는데 값이 없다 = 사라졌다
                continue
            self.fields[name] = FieldValue(name, self.spec.types[name], value, sources=[])
        return changed
