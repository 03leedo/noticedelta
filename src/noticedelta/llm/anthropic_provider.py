"""Claude API provider. 구조화 출력(output_config.format)으로 JSON을 받는다."""

from __future__ import annotations

import json
from typing import Any

from noticedelta.llm.gateway import LLMRequest, LLMResponse

DEFAULT_MODEL = "claude-haiku-5-5"

# 100만 토큰당 USD (입력, 출력). Haiku 5.5는 프롬프트 10만 토큰 이하 기준.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
}


class AnthropicProvider:
    def __init__(
        self,
        client: Any = None,
        *,
        prices: dict[str, tuple[float, float]] | None = None,
        max_tokens: int = 16000,
        effort: str = "low",
    ):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.prices = prices or PRICES
        self.max_tokens = max_tokens
        self.effort = effort

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self.client.messages.create(
            model=req.model,
            max_tokens=self.max_tokens,
            system=req.system,
            messages=[{"role": "user", "content": req.input}],
            output_config={
                "format": {"type": "json_schema", "schema": req.output_schema},
                "effort": self.effort,
            },
        )
        if resp.stop_reason != "end_turn":
            raise RuntimeError(f"LLM 응답이 정상 종료되지 않았다: stop_reason={resp.stop_reason}")
        text = next(b.text for b in resp.content if b.type == "text")
        price_in, price_out = self.prices.get(req.model, (0.0, 0.0))
        cost = (
            resp.usage.input_tokens * price_in + resp.usage.output_tokens * price_out
        ) / 1_000_000
        return LLMResponse(
            json.loads(text), resp.usage.input_tokens, resp.usage.output_tokens, cost
        )
