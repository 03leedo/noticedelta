"""사용자가 등록한 출처 (source-catalog.md 4장): 나만 쓰는 출처(private)로 시작한다.

    register(session, sheet, owner=pid, at=now)   출처 한 장을 넣고 수집을 시작할 수 있게 한다
    sheets(session, owner=pid)                    그 사람의 출처(없으면 모두)
    to_record_for(session, source_id)             적재가 쓰는 변환기(rss)

- 운영자 출처(코드 어댑터)와 id가 겹치면 안 된다. 출처 표(source)에도 한 줄을 둔다(게시물의 외래 키).
- 사용자 출처는 소유자의 앱만 쓴다(앱 정의를 검사할 때 그 사람의 출처만 더한다). 공개는 운영자가
  검토해 올린 뒤(verified)에만.
- 등록은 이용 조건에 동의하는 순간이라 도구 게이트웨이에서 사람의 확인을 거친다(agent.py의 도구).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.catalog import RssConnector, SourceSheet
from noticedelta.db.models import Source, UserSourceRow
from noticedelta.sources.base import NoticeRecord
from noticedelta.sources.rss import _public, check_url
from noticedelta.sources.rss import to_record as rss_record

MAX_ID = 32  # 출처 표의 id 칸
MIN_EVERY_HOURS = 1  # 사용자 출처의 수집 주기 하한(6장)
MAX_RATE = 1.0  # 초당 1회


class SourceTaken(ValueError):
    """이미 있는 출처 id."""


def _ready(session: Session) -> bool:
    """표가 없는 예전 DB(마이그레이션 없이 읽는 스크립트)에서는 사용자 출처가 없는 것으로 본다."""
    return sa.inspect(session.connection()).has_table(UserSourceRow.__tablename__)


def sheets(session: Session, *, owner: str | None = None) -> dict[str, SourceSheet]:
    if not _ready(session):
        return {}
    q = sa.select(UserSourceRow).where(UserSourceRow.removed_at.is_(None))
    if owner is not None:
        q = q.where(UserSourceRow.owner == owner)
    return {r.id: SourceSheet.model_validate(r.body) for r in session.scalars(q)}


def problems(sheet: SourceSheet, taken: Collection[str]) -> list[str]:
    """사용자 출처로 받을 수 있는지. 문제마다 고칠 방법을 함께 적는다."""
    out = []
    if not isinstance(sheet.connector, RssConnector):
        out.append("connector.kind: 사용자 출처는 지금 rss만 받는다")
    if sheet.status != "private":
        out.append("status: 사용자 출처는 private로 시작한다(공개는 운영자가 검토한 뒤)")
    if len(sheet.id) > MAX_ID:
        out.append(f"id: {MAX_ID}자까지")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", sheet.id):
        out.append("id: 영문 소문자·숫자·_만 쓴다(앱 정의의 sources가 이 id를 쓴다)")
    if sheet.id in taken:
        out.append(f"id: {sheet.id}는 이미 있는 출처다. 다른 이름을 쓴다")
    if sheet.fetch.every_hours < MIN_EVERY_HOURS or sheet.fetch.rate_per_second > MAX_RATE:
        out.append(f"fetch: {MIN_EVERY_HOURS}시간마다·초당 {MAX_RATE:g}회를 넘지 못한다")
    if sheet.attachments is not None:
        out.append("attachments: 사용자 출처의 첨부는 아직 받지 않는다")
    return out


def taken_ids(session: Session, operator: Collection[str]) -> set[str]:
    rows = session.scalars(sa.select(Source.id)).all()
    users = session.scalars(sa.select(UserSourceRow.id)).all() if _ready(session) else []
    return set(operator) | set(rows) | set(users)


def register(
    session: Session,
    sheet: SourceSheet,
    *,
    owner: str,
    at: datetime,
    operator: Collection[str] = (),
    resolve: Callable[[str], bool] = _public,
) -> UserSourceRow:
    """넣는다. 고칠 것이 있으면 ValueError(그 목록)."""
    found = problems(sheet, taken_ids(session, operator))
    if found:
        raise (
            SourceTaken("; ".join(found))
            if any("이미 있는" in f for f in found)
            else ValueError("; ".join(found))
        )
    check_url(sheet.connector.url, resolve=resolve)  # 등록 때 한 번 더(받을 때마다 다시 본다)
    body = sheet.model_dump(mode="json", by_alias=True, exclude_none=True)
    row = UserSourceRow(id=sheet.id, owner=owner, body=body, registered_at=at)
    session.add(Source(id=sheet.id, name=sheet.title[:100], base_url=sheet.connector.url[:500]))
    session.add(row)
    session.flush()
    return row


def remove(session: Session, source_id: str, *, owner: str, at: datetime) -> bool:
    """수집을 멈춘다(받아 둔 게시물은 남는다). 소유자만."""
    row = session.get(UserSourceRow, source_id)
    if row is None or row.owner != owner or row.removed_at is not None:
        return False
    row.removed_at = at
    return True


def to_record_for(session: Session, source_id: str) -> Callable[[dict], NoticeRecord] | None:
    sheet = sheets(session).get(source_id)
    if sheet is None or not isinstance(sheet.connector, RssConnector):
        return None
    return rss_record(source_id)
