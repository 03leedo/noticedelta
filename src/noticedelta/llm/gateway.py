"""LLM 게이트웨이: 모든 LLM 호출의 단일 통로 (설계 10.1).

녹화(record)·재생(replay)으로 누가 돌려도 같은 결과가 나오게 하고, 호출·토큰·비용을 집계한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from noticedelta.llm.cassette import CassetteStore


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
    def __init__(self, provider: Provider | None, store: CassetteStore, mode: Mode):
        self.provider = provider
        self.store = store
        self.mode = Mode(mode)
        self.usage = UsageTotals()

    def _live(self, req: LLMRequest) -> LLMResponse:
        if self.provider is None:
            raise RuntimeError(f"{self.mode} 모드에서 실제 호출이 필요한데 provider가 없다")
        resp = self.provider.complete(req)
        resp.from_cassette = False
        self.usage.live_calls += 1
        self.usage.live_cost_usd += resp.cost_usd
        return resp

    def complete(self, req: LLMRequest) -> LLMResponse:
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
        return resp
