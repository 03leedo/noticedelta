"""환경 변수로 LLM 공급자와 모델을 고른다.

NOTICEDELTA_LLM_PROVIDER  anthropic(기본) 또는 pickle
ANTHROPIC_API_KEY         anthropic일 때
PICKLE_API_KEY            pickle일 때. pickle- 로 시작한다
PICKLE_BASE_URL           기본 https://llm.pcl.kr/v1
PICKLE_MODEL              기본 pickle-general(학교 모델, 무료). 모델 목록은 GET {BASE_URL}/models
PICKLE_ALLOW_PAID         1이면 pickle-general 밖의 모델(키의 달러 한도에서 빠짐)을 허용한다

유료 모델은 실수로 쓰지 않게 기본으로 막는다. 녹화본 키에 모델 이름이 들어가므로 공급자를
바꾸면 녹화본도 따로 쌓인다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from noticedelta.llm.anthropic_provider import DEFAULT_MODEL, AnthropicProvider
from noticedelta.llm.gateway import Provider
from noticedelta.llm.openai_compat import (
    PICKLE_BASE_URL,
    PICKLE_FREE_MODEL,
    OpenAICompatProvider,
    ProviderError,
)

PROVIDERS = ("anthropic", "pickle")


@dataclass(frozen=True)
class Choice:
    name: str
    model: str


def choose(name: str | None = None, model: str | None = None) -> Choice:
    """공급자와 모델을 정한다. 호출은 하지 않으므로 녹화본 재생에도 쓴다."""
    name = name or os.environ.get("NOTICEDELTA_LLM_PROVIDER") or "anthropic"
    if name not in PROVIDERS:
        raise ProviderError(f"모르는 LLM 공급자: {name} (가능: {', '.join(PROVIDERS)})")
    if name == "pickle":
        model = model or os.environ.get("PICKLE_MODEL") or PICKLE_FREE_MODEL
        if model != PICKLE_FREE_MODEL and os.environ.get("PICKLE_ALLOW_PAID") != "1":
            raise ProviderError(
                f"{model}은 유료 모델이다. 쓰려면 PICKLE_ALLOW_PAID=1 (무료는 {PICKLE_FREE_MODEL})"
            )
        return Choice(name, model)
    return Choice(name, model or DEFAULT_MODEL)


def make_provider(choice: Choice) -> Provider:
    if choice.name == "pickle":
        return OpenAICompatProvider(
            os.environ.get("PICKLE_API_KEY", ""),
            base_url=os.environ.get("PICKLE_BASE_URL") or PICKLE_BASE_URL,
        )
    return AnthropicProvider()
