"""에이전트에게 내놓는 도구를 모아 도구 게이트웨이를 만든다 (platform-services 5.8).

MCP 서버(scripts/mcp_server.py)와 워커(보류 시간이 지난 효과를 실행한다)가 같은 도구 목록을 쓴다.
모든 호출은 게이트웨이를 지난다. A만 가진 읽기 도구도 지나야 세션이 "외부 글을 봤다"(A)를 안다.

앱이 쓸 도구를 고르는 칸은 아직 앱 정의에 없다. 지금은 지원사업 앱이 모든 도구를 고른 것으로 본다.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Sequence
from datetime import date
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.app_tools import Preview, app_tools, backtest_preview
from noticedelta.appspec import AppDefinition
from noticedelta.db.models import Notice, NoticeVersion
from noticedelta.doc_tools import doc_tools
from noticedelta.grants import APP
from noticedelta.item_tools import item_tools
from noticedelta.mail_tools import FileOutbox, Outbox, inquiry_tool
from noticedelta.parsing import Parser
from noticedelta.person_tools import person_tools
from noticedelta.programs import api_contacts, api_meta
from noticedelta.tool_gateway import ToolGateway
from noticedelta.tooling import Tool
from noticedelta.usage import UsageLog, feedback_tool
from noticedelta.vault import Cipher

ROOT = Path(__file__).resolve().parents[2]
OUTBOX = ROOT / "data" / "outbox"  # 개발용: 메일을 보내지 않고 .eml로 쓴다
CASSETTES = ROOT / "data" / "cassettes"
DAILY_ENV = "NOTICEDELTA_LLM_DAILY_TOKENS"  # 워커와 같은 하루 전체 한도
INQUIRY_KINDS = {
    "deadline": "접수 기간과 마감",
    "documents": "제출 서류",
    "eligibility": "신청 자격",
    "other": "내용",
}


def item_contacts(db: Session, args: dict) -> set[str]:
    """항목의 게시물들이 출처 API의 문의처 칸에 적은 주소(받는 쪽 허용 목록)."""
    rows = db.execute(
        sa.select(Notice.source_id, NoticeVersion.raw)
        .join(NoticeVersion, NoticeVersion.notice_id == Notice.id)
        .where(Notice.item_id == str(args.get("item_id")))
    ).all()
    out: set[str] = set()
    for source, raw in rows:
        out |= api_contacts(source, raw or {})
    return out


def agent_tools(
    factory: Callable[[], Session],
    *,
    parser: Parser | None = None,
    roots: Sequence[Path] = (),
    usage: UsageLog | None = None,
    app: AppDefinition = APP,
    today: Callable[[], date] = date.today,
    outbox: Outbox | None = None,
    preview: Preview | None = None,
) -> list[Tool]:
    tools = [
        *doc_tools(parser, roots),
        *item_tools(factory, meta=api_meta),
        *person_tools(factory, app, today=today),
        inquiry_tool(
            factory, outbox or FileOutbox(OUTBOX), allow=item_contacts, kinds=INQUIRY_KINDS
        ),
        *app_tools(
            factory,
            preview=preview or backtest_preview(CASSETTES, factory),
            snapshots=ROOT / "data" / "snapshots",
            daily=int(os.environ.get(DAILY_ENV, "200000")),
        ),
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
    outbox: Outbox | None = None,
    preview: Preview | None = None,
) -> ToolGateway:
    tools = agent_tools(
        factory,
        parser=parser,
        roots=roots,
        usage=usage,
        app=app,
        today=today,
        outbox=outbox,
        preview=preview,
    )
    return ToolGateway(
        tools,
        chosen=[t.name for t in tools] if chosen is None else chosen,
        cipher=cipher,
        observer=usage,
    )
