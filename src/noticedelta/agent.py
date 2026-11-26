"""에이전트에게 내놓는 도구를 모아 도구 게이트웨이를 만든다 (platform-services 5.8).

MCP 서버(scripts/mcp_server.py)와 워커(보류 시간이 지난 효과를 실행한다)가 같은 도구 목록을 쓴다.
모든 호출은 게이트웨이를 지난다. A만 가진 읽기 도구도 지나야 세션이 "외부 글을 봤다"(A)를 안다.

앱이 쓸 도구를 고르는 칸은 아직 앱 정의에 없다. 지금은 지원사업 앱이 모든 도구를 고른 것으로 본다.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from noticedelta.appspec import AppDefinition
from noticedelta.doc_tools import doc_tools
from noticedelta.grants import APP
from noticedelta.item_tools import item_tools
from noticedelta.parsing import Parser
from noticedelta.person_tools import person_tools
from noticedelta.programs import api_meta
from noticedelta.tool_gateway import ToolGateway
from noticedelta.tooling import Tool
from noticedelta.usage import UsageLog, feedback_tool
from noticedelta.vault import Cipher


def agent_tools(
    factory: Callable[[], Session],
    *,
    parser: Parser | None = None,
    roots: Sequence[Path] = (),
    usage: UsageLog | None = None,
    app: AppDefinition = APP,
    today: Callable[[], date] = date.today,
) -> list[Tool]:
    tools = [
        *doc_tools(parser, roots),
        *item_tools(factory, meta=api_meta),
        *person_tools(factory, app, today=today),
    ]
    if usage is not None:
        tools.append(feedback_tool(usage))
    return tools


def build_gateway(
    factory: Callable[[], Session],
    *,
    parser: Parser | None = None,
    roots: Sequence[Path] = (),
    cipher: Cipher | None = None,
    usage: UsageLog | None = None,
    app: AppDefinition = APP,
    chosen: Collection[str] | None = None,
    today: Callable[[], date] = date.today,
) -> ToolGateway:
    tools = agent_tools(factory, parser=parser, roots=roots, usage=usage, app=app, today=today)
    return ToolGateway(
        tools,
        chosen=[t.name for t in tools] if chosen is None else chosen,
        cipher=cipher,
        observer=usage,
    )
