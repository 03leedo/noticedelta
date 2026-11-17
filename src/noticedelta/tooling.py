"""도구: 플랫폼 기능을 에이전트가 부를 수 있는 모양으로 내놓는다 (ARCHITECTURE 5.2, 7장).

도구마다 능력 속성을 선언한다.

    A  신뢰할 수 없는 입력(공고·첨부의 글)을 돌려준다
    B  민감 데이터(프로필·서류)를 읽는다
    C  밖으로 나가거나 되돌릴 수 없는 일을 한다

등록부(Registry)는 허용한 속성 안에 드는 도구만 내놓는다. 도구 게이트웨이(세션 상태, 승인,
보류 후 확정)가 생기기 전까지는 A만 가진 읽기 도구만 내놓고, B·C가 붙은 도구는 받아 두기만 한다.
도구가 돌려주는 글은 인젝션 탐지를 거쳐, 지시로 보이는 줄을 가린다.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from noticedelta.llm.harness import NEUTRALIZED, scan

CAPABILITIES = {
    "A": "신뢰할 수 없는 입력을 돌려준다",
    "B": "민감 데이터를 읽는다",
    "C": "밖으로 나가거나 되돌릴 수 없는 일을 한다",
}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[
        ..., dict
    ]  # 인자는 타입을 적은 키워드 인자, 결과는 JSON으로 바꿀 수 있는 사전
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = set(self.capabilities) - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"모르는 능력 속성: {sorted(unknown)}")


def guard(value: Any) -> tuple[Any, int]:
    """결과 안의 글에서 지시로 보이는 줄을 가린다. (가린 결과, 가린 줄 수)."""
    if isinstance(value, str):
        lines = value.split("\n")
        hits = [i for i, line in enumerate(lines) if scan(line)]
        for i in hits:
            lines[i] = NEUTRALIZED
        return ("\n".join(lines) if hits else value), len(hits)
    if isinstance(value, list):
        pairs = [guard(v) for v in value]
        return [v for v, _ in pairs], sum(n for _, n in pairs)
    if isinstance(value, dict):
        pairs = {k: guard(v) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, sum(n for _, n in pairs.values())
    return value, 0


class Registry:
    def __init__(self, allowed: Collection[str] = ("A",)):
        self.allowed = frozenset(allowed)
        self.tools: dict[str, Tool] = {}
        self.withheld: dict[str, Tool] = {}  # 허용하지 않은 속성이 붙어 내놓지 않는 도구
        self.hidden_lines = 0  # 지금까지 가린 줄 수

    def add(self, tool: Tool) -> None:
        if tool.name in self.tools or tool.name in self.withheld:
            raise ValueError(f"같은 이름의 도구가 이미 있다: {tool.name}")
        (self.tools if tool.capabilities <= self.allowed else self.withheld)[tool.name] = tool

    def call(self, name: str, **arguments: Any) -> dict:
        if name not in self.tools:
            reason = "게이트웨이 없이는 내놓지 않는 도구" if name in self.withheld else "없는 도구"
            raise KeyError(f"{reason}: {name}")
        result, hidden = guard(self.tools[name].handler(**arguments))
        if hidden:
            self.hidden_lines += hidden
            result["guard"] = {"hidden_lines": hidden, "why": "지시로 보이는 줄을 가렸습니다"}
        return result

    def bound(self, name: str) -> Callable[..., dict]:
        """등록부를 거쳐 부르는 함수. 인자 모양(서명)은 원래 도구와 같다."""
        handler = self.tools[name].handler

        @functools.wraps(handler)
        def call(**arguments: Any) -> dict:
            return self.call(name, **arguments)

        return call
