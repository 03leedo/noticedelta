"""항목에 대해 묻는 메일을 보내는 도구(C: 밖으로 나간다) (platform-services 5장, 6장 7~8번).

- 바로 보내지 않는다. 도구 게이트웨이가 효과로 붙잡고, 승인·보류 시간이 지나야 워커가 보낸다.
- 받는 쪽은 허용 목록(allow) 안이어야 한다. 허용 목록은 앱이 준다(지원사업 앱은 출처 API의 문의처 칸).
- 서식(form)은 항목·받는 쪽·문의 종류다. 세션이 외부 글·내 정보·외부 효과를 모두 갖게 되면(전이)
  자유 글(body)을 빼고 서식으로만 다시 만든다(split). 외부 글의 지시나 내 정보가 본문에 섞이지 않는다.
- 보내는 계정(메일 서버)은 운영자가 outbox를 만들 때 넣는다. 에이전트는 자격증명을 줄 수 없다(5.6).

개발용 outbox(FileOutbox)는 실제로 보내지 않고 .eml 파일로 쓴다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from noticedelta.db.models import Item
from noticedelta.tooling import Tool

MAX_BODY = 2000
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


class Outbox(Protocol):
    def send(self, to: str, subject: str, body: str) -> dict: ...


class FileOutbox:
    """보내지 않고 .eml 파일로 쓴다(개발·시험용)."""

    def __init__(self, folder: Path, *, sender: str = "noticedelta@localhost"):
        self.folder = Path(folder)
        self.sender = sender

    def send(self, to: str, subject: str, body: str) -> dict:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg["Message-ID"] = make_msgid(domain="noticedelta.local")
        msg.set_content(body)
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        path = self.folder / f"{stamp}.eml"
        path.write_bytes(bytes(msg))
        return {"message_id": msg["Message-ID"], "file": path.name}


def inquiry_tool(
    factory: Callable[[], Session],
    outbox: Outbox,
    *,
    allow: Callable[[Session, dict], Collection[str]],
    kinds: Mapping[str, str],
) -> Tool:
    """kinds: 문의 종류 → 본문에 들어갈 문구(예: {"documents": "제출 서류"})."""

    def validate(args: dict) -> str | None:
        if args.get("kind") not in kinds:
            return f"문의 종류는 {sorted(kinds)} 가운데 하나다"
        if not _EMAIL.fullmatch(str(args.get("to", "")).strip()):
            return "받는 쪽은 메일 주소 하나다"
        if len(args.get("body") or "") > MAX_BODY:
            return f"본문은 {MAX_BODY}자까지다"
        return None

    def send_inquiry(item_id: str, to: str, kind: str, body: str = "") -> dict:
        """항목 하나에 대해 기관에 묻는 메일을 보낸다(승인·보류 뒤에 나간다)."""
        with factory() as s:
            item = s.get(Item, item_id)
            title = item.title if item is not None else item_id
        text = f"안녕하세요.\n\n「{title}」 공고의 {kinds[kind]}에 대해 문의드립니다."
        if body.strip():
            text += f"\n\n{body.strip()}"
        text += "\n\n— NoticeDelta를 통해 보낸 문의입니다."
        return outbox.send(to.strip(), f"[문의] {title}", text)

    return Tool(
        "send_inquiry",
        "항목(공고) 하나에 대해 기관의 문의처로 메일을 보낸다. 바로 나가지 않고, 필요하면 사람의 승인을"
        " 받은 뒤 보류 시간이 지나야 나간다. 받는 쪽은 출처가 알려 준 문의처만 된다.",
        send_inquiry,
        frozenset({"C"}),
        effect="external",
        targets=("to",),
        allow=allow,
        form=("item_id", "to", "kind"),
        validate=validate,
        logged_args=("item_id", "kind"),
    )
