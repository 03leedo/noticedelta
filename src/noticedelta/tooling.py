"""도구: 플랫폼 기능을 에이전트가 부를 수 있는 모양으로 내놓는다 (ARCHITECTURE 5.2, 7장).

도구마다 능력 속성을 선언한다.

    A  신뢰할 수 없는 입력(공고·첨부의 글)을 돌려준다
    B  민감 데이터(프로필·서류)를 읽는다
    C  밖으로 나가거나 되돌릴 수 없는 일을 한다

도구마다 효과도 선언한다(platform-services 5.2). none(읽기만), internal(플랫폼 안의 되돌릴 수 있는
상태, 되돌리는 함수가 있어야 한다), external(밖으로 나간다, C). 선언은 코드에 두고 운영자가 고정한다.

등록부(Registry)는 효과가 없고 A만 가진 읽기 도구만 내놓는다. B·C가 붙었거나 효과가 있는 도구는 받아
두기만 하고, 도구 게이트웨이(tool_gateway.ToolGateway)만 그것을 부른다.
도구가 돌려주는 글은 인젝션 탐지를 거쳐, 지시로 보이는 줄을 가린다.
관찰자(observer)를 주면 호출마다 알려 준다. 사용 기록(usage.py)이 이것으로 남긴다.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from noticedelta.llm.harness import NEUTRALIZED, scan

CAPABILITIES = {
    "A": "신뢰할 수 없는 입력을 돌려준다",
    "B": "민감 데이터를 읽는다",
    "C": "밖으로 나가거나 되돌릴 수 없는 일을 한다",
}
EFFECTS = ("none", "internal", "external")
READ_ONLY = frozenset({"A"})  # 등록부가 내놓을 수 있는 속성. B·C는 게이트웨이로만


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[
        ..., dict
    ]  # 인자는 타입을 적은 키워드 인자, 결과는 JSON으로 바꿀 수 있는 사전
    capabilities: frozenset[str] = frozenset()
    version: str = "1"  # 설명·입력·기본값을 바꾸면 올린다. 사용 기록을 판별로 비교한다
    count: str | None = None  # 결과 개수를 담은 칸(수 또는 목록). "결과 없음"을 셀 때 쓴다
    logged_args: tuple[str, ...] = ()  # 사용 기록에 남겨도 되는 인자(개인정보는 가리고 자른다)
    # --- 게이트웨이가 보는 칸 (platform-services 5.2) ---
    effect: str = "none"  # none / internal / external(C)
    undo: Callable[..., Any] | None = None  # internal: 결과의 undo 정보를 받아 되돌린다
    purpose: str | None = None  # B: 금고에서 쓰는 용도(동의의 용도)
    keys: tuple[str, ...] = ()  # B: 금고의 키. 게이트웨이가 꺼내 profile 인자로 넣는다
    hold: timedelta | None = None  # external: 확정 전에 취소할 수 있는 시간(없으면 게이트웨이 기본)
    targets: tuple[str, ...] = ()  # external: 받는 쪽 인자(받는 사람, 주소)
    allow: Callable[..., Collection[str]] | None = None  # external: (db, 인자) → 받는 쪽 허용 목록
    form: tuple[str, ...] = ()  # external: 서식 칸. 있으면 전이 때 이 칸만으로 다시 만든다(split)
    max_calls: int | None = None  # 세션 하나에서 부를 수 있는 수

    def __post_init__(self) -> None:
        caps = set(self.capabilities)
        unknown = caps - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"모르는 능력 속성: {sorted(unknown)}")
        if self.effect not in EFFECTS:
            raise ValueError(f"모르는 효과: {self.effect}")
        if ("C" in caps) != (self.effect == "external"):
            raise ValueError(f"{self.name}: C는 밖으로 나가는 효과(external)와 함께 선언한다")
        if (self.undo is not None) != (self.effect == "internal"):
            raise ValueError(f"{self.name}: internal 효과에는 되돌리는 함수(undo)가 있어야 한다")
        if ("B" in caps) != (self.purpose is not None) or ("B" in caps and not self.keys):
            raise ValueError(f"{self.name}: B 도구는 금고의 용도(purpose)와 키(keys)를 선언한다")
        if {"B", "C"} <= caps:
            raise ValueError(f"{self.name}: 민감 데이터(B)와 외부 효과(C)를 한 도구에 두지 않는다")
        external_only = self.hold or self.targets or self.allow or self.form
        if external_only and self.effect != "external":
            raise ValueError(f"{self.name}: hold·targets·allow·form은 external 도구의 칸이다")
        if self.targets and self.allow is None:
            raise ValueError(f"{self.name}: 받는 쪽(targets)이 있으면 허용 목록(allow)도 둔다")
        if self.form and not set(self.targets) <= set(self.form):
            raise ValueError(f"{self.name}: 서식(form)에 받는 쪽 칸이 들어 있어야 한다")

    @property
    def read_only(self) -> bool:
        """효과가 없고 A만 가진 도구: 게이트웨이 없이 등록부로 내놓을 수 있다."""
        return self.effect == "none" and self.capabilities <= READ_ONLY


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


class Observer(Protocol):
    def record(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        result: dict | None,
        error: BaseException | None,
        duration_ms: int,
        hidden_lines: int,
    ) -> None: ...


class Registry:
    def __init__(self, allowed: Collection[str] = ("A",), observer: Observer | None = None):
        self.allowed = frozenset(allowed)
        if not self.allowed <= READ_ONLY:
            raise ValueError(
                "B·C 도구는 등록부로 내놓지 않는다. 도구 게이트웨이(ToolGateway)를 쓴다"
            )
        self.observer = observer
        self.tools: dict[str, Tool] = {}
        self.withheld: dict[str, Tool] = {}  # 허용하지 않은 속성이 붙어 내놓지 않는 도구
        self.hidden_lines = 0  # 지금까지 가린 줄 수

    def add(self, tool: Tool) -> None:
        if tool.name in self.tools or tool.name in self.withheld:
            raise ValueError(f"같은 이름의 도구가 이미 있다: {tool.name}")
        shown = tool.read_only and tool.capabilities <= self.allowed
        (self.tools if shown else self.withheld)[tool.name] = tool

    def call(self, name: str, **arguments: Any) -> dict:
        if name not in self.tools:
            reason = "게이트웨이 없이는 내놓지 않는 도구" if name in self.withheld else "없는 도구"
            raise KeyError(f"{reason}: {name}")
        tool = self.tools[name]
        started = time.perf_counter()
        try:
            result, hidden = guard(tool.handler(**arguments))
        except Exception as e:
            self._observe(tool, arguments, None, e, started, 0)
            raise
        if hidden:
            self.hidden_lines += hidden
            result["guard"] = {"hidden_lines": hidden, "why": "지시로 보이는 줄을 가렸습니다"}
        self._observe(tool, arguments, result, None, started, hidden)
        return result

    def _observe(self, tool, arguments, result, error, started, hidden) -> None:
        if self.observer is None:
            return
        ms = int((time.perf_counter() - started) * 1000)
        self.observer.record(
            tool, arguments, result=result, error=error, duration_ms=ms, hidden_lines=hidden
        )

    def bound(self, name: str) -> Callable[..., dict]:
        """등록부를 거쳐 부르는 함수. 인자 모양(서명)은 원래 도구와 같다."""
        handler = self.tools[name].handler

        @functools.wraps(handler)
        def call(**arguments: Any) -> dict:
            return self.call(name, **arguments)

        return call
