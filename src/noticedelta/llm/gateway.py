"""LLM 게이트웨이: 모든 LLM 호출의 단일 통로 (설계 10.1).

녹화(record)·재생(replay)으로 누가 돌려도 같은 결과가 나오게 하고, 호출·토큰·비용을 집계한다.
토큰을 아끼는 두 장치도 여기 있다. 미리 세기(`Mode.PLAN`)는 녹화본에 없는 호출을 보내지 않고
크기만 세어 "이번 실행에 토큰이 얼마나 드나"를 알려 준다. 예산(`Budget`)은 여러 게이트웨이가 함께
쓰는 상한으로, 넘을 호출은 보내기 전에 막는다(BudgetExceeded).
하네스(llm/harness.py)를 주면 요청을 보내기 전과 응답을 받은 뒤에 끼워 돌린다. 녹화본의 키와
내용은 하네스를 거친 요청으로 만들므로, 하네스가 가린 값은 녹화본에 남지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from noticedelta.llm.cassette import CassetteStore
from noticedelta.llm.harness import Harness, Trace


class Mode(StrEnum):
    LIVE = "live"  # 그냥 호출
    RECORD = "record"  # 호출하고 응답 저장
    REPLAY = "replay"  # 저장된 응답만, 없으면 실패
    REPLAY_OR_LIVE = "replay_or_live"  # 저장된 응답, 없으면 호출 후 저장
    PLAN = "plan"  # 저장된 응답은 쓰고, 없으면 부르지 않고 크기만 센 뒤 빈 응답을 낸다


# 글자 수로 토큰을 어림하는 비율. 피클 학교 모델 녹화본 90개에서 잰 값(0.61~0.66)보다
# 조금 높게 잡는다. 예산은 넘치는 것보다 남는 편이 낫다. 녹화본이 있으면 calibrate()로 잰 값을 쓴다.
TOKENS_PER_CHAR = 0.7


@dataclass(frozen=True)
class LLMRequest:
    model: str
    template_version: str
    system: str
    input: str
    output_schema: dict


@dataclass
class LLMResponse:
    output: dict
    input_tokens: int
    output_tokens: int
    cost_usd: float
    from_cassette: bool = False


class Provider(Protocol):
    def complete(self, req: LLMRequest) -> LLMResponse: ...


class MissingCassette(Exception):
    """REPLAY 모드에서 녹화본이 없다."""


class BudgetExceeded(Exception):
    """이 호출을 보내면 예산을 넘는다. 보내지 않았다."""


@dataclass
class Budget:
    """실제로 보낸 호출의 입력+출력 토큰 상한. 여러 게이트웨이가 같은 것을 나눠 쓸 수 있다."""

    limit: int
    spent: int = 0
    blocked: bool = False  # 한 번이라도 넘어 막힌 적이 있다(워커가 더 꺼내지 않는다)

    def check(self, estimate: int) -> None:
        if self.spent + estimate > self.limit:
            self.blocked = True
            raise BudgetExceeded(
                f"토큰 예산 {self.limit:,} 중 {self.spent:,} 씀, 이번 호출 어림 {estimate:,}"
            )


def request_chars(req: LLMRequest) -> int:
    schema = json.dumps(req.output_schema, ensure_ascii=False, separators=(",", ":"))
    return len(req.system) + len(req.input) + len(schema)


def estimate_tokens(req: LLMRequest, tokens_per_char: float = TOKENS_PER_CHAR) -> int:
    """보내기 전에 입력 토큰을 어림한다. 공급자의 토크나이저 없이 글자 수로만."""
    return math.ceil(request_chars(req) * tokens_per_char)


def calibrate(records: Iterable[dict], model: str, template_version: str) -> float | None:
    """녹화본에서 (모델, 틀)의 실제 토큰/글자 비율을 잰다. 같은 것이 없으면 None."""
    tokens = chars = 0
    for r in records:
        q = r.get("request", {})
        if q.get("model") != model or q.get("template_version") != template_version:
            continue
        tokens += r.get("response", {}).get("input_tokens", 0)
        chars += request_chars(LLMRequest(**q))
    return tokens / chars if tokens and chars else None


@dataclass
class UsageTotals:
    calls: int = 0
    live_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # 녹화본 포함, 실제로 호출했다면 들었을 비용
    live_cost_usd: float = 0.0  # 이번 실행에서 실제로 쓴 비용
    live_tokens: int = 0  # 이번 실행에서 실제로 보낸 호출의 입력+출력 토큰(실패한 호출 포함)
    failed_calls: int = 0  # 보냈지만 쓸 수 있는 답을 받지 못한 호출
    planned_calls: int = 0  # PLAN 모드에서 녹화본이 없어 보냈어야 할 호출 수
    planned_tokens: int = 0  # 그 호출들의 입력 토큰 어림


def request_key(req: LLMRequest) -> str:
    canonical = json.dumps(
        {
            "model": req.model,
            "template_version": req.template_version,
            "input": [req.system, req.input],
            "output_schema": req.output_schema,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Gateway:
    def __init__(
        self,
        provider: Provider | None,
        store: CassetteStore,
        mode: Mode,
        harnesses: Sequence[Harness] = (),
        *,
        budget: Budget | None = None,
        tokens_per_char: float = TOKENS_PER_CHAR,
        observer: Callable[[str, LLMRequest, LLMResponse], None] | None = None,
    ):
        self.provider = provider
        self.observer = observer  # 호출마다 (녹화본 키, 요청, 응답)을 받는다. 비행 기록기가 쓴다
        self.budget = budget
        self.tokens_per_char = tokens_per_char
        self._planned: set[str] = set()
        self.store = store
        self.mode = Mode(mode)
        self.usage = UsageTotals()
        self.harnesses = list(harnesses)
        # 하네스가 남긴 것의 합계. 예: {"pii.PHONE": 가린 수, "injection.override": 의심 줄 수}
        self.findings: Counter[str] = Counter()
        self.last_trace = Trace()  # 마지막 호출의 기록(부른 쪽이 의심 내용을 보고 싶을 때)

    def _live(self, req: LLMRequest) -> LLMResponse:
        if self.provider is None:
            raise RuntimeError(f"{self.mode} 모드에서 실제 호출이 필요한데 provider가 없다")
        if self.budget is not None:
            self.budget.check(estimate_tokens(req, self.tokens_per_char))
        try:
            resp = self.provider.complete(req)
        except Exception as e:
            # 실패해도 공급자가 센 토큰은 한도에서 빠졌다. 예산과 합계에 넣고 다시 올린다
            lost = getattr(e, "spent", 0)
            if self.budget is not None:
                self.budget.spent += lost
            self.usage.failed_calls += 1
            self.usage.live_tokens += lost
            raise
        resp.from_cassette = False
        spent = resp.input_tokens + resp.output_tokens
        if self.budget is not None:
            self.budget.spent += spent
        self.usage.live_calls += 1
        self.usage.live_cost_usd += resp.cost_usd
        self.usage.live_tokens += spent
        return resp

    def _plan(self, key: str, req: LLMRequest) -> LLMResponse:
        # 같은 요청은 실제로 돌리면 처음 한 번만 나가고 뒤는 방금 녹화한 것을 쓴다. 한 번만 센다.
        if key not in self._planned:
            self._planned.add(key)
            self.usage.planned_calls += 1
            self.usage.planned_tokens += estimate_tokens(req, self.tokens_per_char)
        return LLMResponse(output={}, input_tokens=0, output_tokens=0, cost_usd=0.0)

    def complete(self, req: LLMRequest) -> LLMResponse:
        trace = self.last_trace = Trace()
        try:
            for h in self.harnesses:
                req = h.before(req, trace)  # 막는 하네스는 여기서 Blocked를 올린다
        finally:
            for f in trace.findings:
                self.findings[f"{f.harness}.{f.kind}"] += f.count
        key = request_key(req)
        resp: LLMResponse | None = None
        if self.mode in (Mode.REPLAY, Mode.REPLAY_OR_LIVE, Mode.PLAN):
            saved = self.store.load(key)
            if saved is not None:
                resp = LLMResponse(**saved["response"], from_cassette=True)
            elif self.mode is Mode.REPLAY:
                raise MissingCassette(key)
            elif self.mode is Mode.PLAN:
                resp = self._plan(key, req)
        if resp is None:
            resp = self._live(req)
            if self.mode in (Mode.RECORD, Mode.REPLAY_OR_LIVE):
                body = asdict(resp)
                body.pop("from_cassette")
                self.store.save(key, {"key": key, "request": asdict(req), "response": body})
        self.usage.calls += 1
        self.usage.input_tokens += resp.input_tokens
        self.usage.output_tokens += resp.output_tokens
        self.usage.cost_usd += resp.cost_usd
        for h in reversed(self.harnesses):
            resp = h.after(resp, trace)
        if self.observer is not None:
            self.observer(key, req, resp)
        return resp
