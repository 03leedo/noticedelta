"""LLM 게이트웨이: 모든 LLM 호출의 단일 통로 (설계 10.1).

녹화(record)·재생(replay)으로 누가 돌려도 같은 결과가 나오게 하고, 호출·토큰·비용을 집계한다.
하네스(llm/harness.py)를 주면 요청을 보내기 전과 응답을 받은 뒤에 끼워 돌린다. 녹화본의 키와
내용은 하네스를 거친 요청으로 만들므로, 하네스가 가린 값은 녹화본에 남지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
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


@dataclass
class UsageTotals:
    calls: int = 0
    live_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # 녹화본 포함, 실제로 호출했다면 들었을 비용
    live_cost_usd: float = 0.0  # 이번 실행에서 실제로 쓴 비용


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
    ):
        self.provider = provider
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
        resp = self.provider.complete(req)
        resp.from_cassette = False
        self.usage.live_calls += 1
        self.usage.live_cost_usd += resp.cost_usd
        return resp

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
        if self.mode in (Mode.REPLAY, Mode.REPLAY_OR_LIVE):
            saved = self.store.load(key)
            if saved is not None:
                resp = LLMResponse(**saved["response"], from_cassette=True)
            elif self.mode is Mode.REPLAY:
                raise MissingCassette(key)
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
        return resp
