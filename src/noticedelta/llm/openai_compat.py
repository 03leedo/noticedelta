"""OpenAI 호환 chat/completions 공급자. 부산대 피클 LLM 게이트웨이(https://llm.pcl.kr/v1)용.

구조화 출력은 response_format json_schema(strict)로 받는다. 피클의 학교 모델(pickle-general,
vLLM)과 유료 모델 모두 이 방식을 받는다(2026-11-24 실측).

비용: 학교 모델은 토큰 한도만 있고 돈이 들지 않는다(0으로 센다). 유료 모델은 응답의
usage.cost(달러)를 쓴다. 키마다 분당 요청·토큰 한도가 있어 429·503이면 Retry-After만큼
기다렸다 다시 보낸다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from noticedelta.llm.gateway import LLMRequest, LLMResponse

PICKLE_BASE_URL = "https://llm.pcl.kr/v1"
PICKLE_FREE_MODEL = "pickle-general"
RETRY_STATUS = {429, 502, 503}


class ProviderError(RuntimeError):
    """공급자가 요청을 거절했거나 응답을 쓸 수 없다."""


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "")), 120.0)
    except ValueError:
        return min(2.0 * 2**attempt, 60.0)


class OpenAICompatProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = PICKLE_BASE_URL,
        client: httpx.Client | None = None,
        max_tokens: int = 16000,
        timeout: float = 300.0,
        retries: int = 4,
        temperature: float | None = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ProviderError("API 키가 비어 있다 (.env의 PICKLE_API_KEY)")
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.max_tokens = max_tokens
        self.retries = retries
        self.temperature = temperature
        self.sleep = sleep

    def body(self, req: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.input},
            ],
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": req.output_schema},
            },
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return body

    def _post(self, body: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        for attempt in range(self.retries + 1):
            resp = self.client.post(url, json=body, headers=self.headers)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                self.sleep(_retry_after(resp, attempt))
                continue
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            code = err.get("code") or resp.status_code
            raise ProviderError(f"{resp.status_code} {code}: {err.get('message', '')[:200]}")
        raise AssertionError("unreachable")

    def complete(self, req: LLMRequest) -> LLMResponse:
        data = self._post(self.body(req))
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ProviderError(
                f"LLM 응답이 정상 종료되지 않았다: finish_reason={choice.get('finish_reason')}"
            )
        text = choice["message"].get("content") or ""
        try:
            output = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"JSON이 아닌 응답: {text[:120]!r}") from e
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        return LLMResponse(
            output, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), cost
        )
