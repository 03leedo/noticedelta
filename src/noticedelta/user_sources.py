"""사용자가 등록한 출처 (source-catalog.md 4장): 나만 쓰는 출처(private)로 시작한다.

    register(session, sheet, owner=pid, at=now)   출처 한 장을 넣고 수집을 시작할 수 있게 한다
    sheets(session, owner=pid)                    그 사람의 출처(없으면 모두)
    to_record_for(session, source_id)             적재가 쓰는 변환기(rss·게시판·파일)
    drop(session, source_id, data, filename, by=pid, at=now)   파일 올리기 출처에 새 판을 올린다

- 운영자 출처(코드 어댑터)와 id가 겹치면 안 된다. 출처 표(source)에도 한 줄을 둔다(게시물의 외래 키).
- 사용자 출처는 소유자의 앱만 쓴다(앱 정의를 검사할 때 그 사람의 출처만 더한다). 공개는 운영자가
  검토해 올린 뒤(verified)에만.
- 등록은 이용 조건에 동의하는 순간이라 도구 게이트웨이에서 사람의 확인을 거친다(agent.py의 도구).
- 파일 올리기(file_drop): 소유자가 목록 파일의 새 판을 올린다. 수집은 거두지 않은 마지막 판을 읽는다.
  같은 파일을 다시 올리면 새 판이 아니다. 사람이 정보를 지우면 그 사람의 출처를 멈추고 올린 파일을 지운다.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Collection
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

import noticedelta.sources.file_drop as file_drop
from noticedelta import events
from noticedelta.catalog import FileDropConnector, HtmlListConnector, RssConnector, SourceSheet
from noticedelta.db.models import Source, SourceDropRow, UserSourceRow
from noticedelta.sources.base import NoticeRecord, SourceError
from noticedelta.sources.html_list import to_record as list_record
from noticedelta.sources.rss import _public, check_url
from noticedelta.sources.rss import to_record as rss_record

log = logging.getLogger(__name__)
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
    out = {}
    for r in session.scalars(q):
        try:
            out[r.id] = SourceSheet.model_validate(r.body)
        except ValueError:  # 장 하나가 깨져도 다른 출처의 적재·비교는 돈다
            log.warning("사용자 출처 %s의 장을 읽지 못해 건너뜁니다", r.id)
    return out


def problems(sheet: SourceSheet, taken: Collection[str]) -> list[str]:
    """사용자 출처로 받을 수 있는지. 문제마다 고칠 방법을 함께 적는다."""
    out = []
    if not isinstance(sheet.connector, RssConnector | HtmlListConnector | FileDropConnector):
        out.append("connector.kind: 사용자 출처는 rss, html_list(게시판), file_drop(파일)만 받는다")
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
    url = getattr(sheet.connector, "url", None)
    if url is not None:
        check_url(url, resolve=resolve)  # 등록 때 한 번 더(받을 때마다 다시 본다)
    body = sheet.model_dump(mode="json", by_alias=True, exclude_none=True)
    row = UserSourceRow(id=sheet.id, owner=owner, body=body, registered_at=at)
    session.add(Source(id=sheet.id, name=sheet.title[:100], base_url=url[:500] if url else None))
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


def full_list(session: Session, source_id: str) -> bool:
    """받을 때마다 목록 전체가 오는 출처인가(파일 올리기의 판). 그러면 빠진 항목을 닫는다."""
    row = session.get(UserSourceRow, source_id) if _ready(session) else None
    return row is not None and (row.body.get("connector") or {}).get("kind") == "file_drop"


def to_record_for(session: Session, source_id: str) -> Callable[[dict], NoticeRecord] | None:
    if not _ready(session):
        return None
    row = session.get(UserSourceRow, source_id)
    if row is None or row.removed_at is not None:
        return None
    try:
        sheet = SourceSheet.model_validate(row.body)
    except ValueError:
        return None
    if isinstance(sheet.connector, HtmlListConnector):
        return list_record(source_id)
    if isinstance(sheet.connector, FileDropConnector):
        return file_drop.to_record(source_id, sheet)
    if not isinstance(sheet.connector, RssConnector):
        return None
    return rss_record(source_id)


# --- 파일 올리기 (file_drop) ---


class NotYours(PermissionError):
    """소유자가 아니거나 없는 출처(남의 private 출처가 있다는 것은 드러내지 않는다)."""


def _file_source(session: Session, source_id: str, owner: str) -> SourceSheet:
    row = session.get(UserSourceRow, source_id) if _ready(session) else None
    if row is None or row.owner != owner or row.removed_at is not None:
        raise NotYours(f"내가 등록한 출처가 아니다: {source_id}")
    sheet = SourceSheet.model_validate(row.body)
    if not isinstance(sheet.connector, FileDropConnector):
        raise ValueError(f"{source_id}는 파일 올리기 출처가 아니다(받아 오는 출처다)")
    return sheet


def check_drop(sheet: SourceSheet, data: bytes) -> list[dict[str, str]]:
    """올릴 수 있는 판인지 보고 줄들을 낸다. 고칠 것이 있으면 SourceError."""
    rows = file_drop.parse(data, sheet.connector.format)
    why = file_drop.id_problem(rows, sheet.item.id)
    if why is not None:
        raise SourceError(why)
    missing = sorted({sheet.item.id, sheet.item.title} - set(file_drop.columns(rows)))
    if missing:
        raise SourceError(f"장에 적은 칸이 파일에 없다: {missing}")
    return rows


def current_drop(session: Session, source_id: str) -> SourceDropRow | None:
    """수집이 읽을 판: 거두지 않은 마지막 판."""
    return session.scalar(
        sa.select(SourceDropRow)
        .where(
            SourceDropRow.source_id == source_id,
            SourceDropRow.withdrawn_at.is_(None),
            SourceDropRow.body.is_not(None),
        )
        .order_by(SourceDropRow.seq.desc())
        .limit(1)
    )


def drops(session: Session, source_id: str) -> list[SourceDropRow]:
    return list(
        session.scalars(
            sa.select(SourceDropRow)
            .where(SourceDropRow.source_id == source_id)
            .order_by(SourceDropRow.seq.desc())
        ).all()
    )


def drop(
    session: Session, source_id: str, data: bytes, filename: str, *, by: str, at: datetime
) -> tuple[SourceDropRow, bool]:
    """새 판을 올린다. (판, 새 판인가). 마지막 판과 같은 파일이면 그 판을 돌려준다."""
    sheet = _file_source(session, source_id, by)
    rows = check_drop(sheet, data)
    digest = hashlib.sha256(data).hexdigest()
    latest = current_drop(session, source_id)
    if latest is not None and latest.sha256 == digest:
        return latest, False
    seq = (
        session.scalar(
            sa.select(sa.func.max(SourceDropRow.seq)).where(SourceDropRow.source_id == source_id)
        )
        or 0
    ) + 1
    row = SourceDropRow(
        id=events.new_ulid(at),
        source_id=source_id,
        seq=seq,
        sha256=digest,
        filename=(filename or "upload")[:200],
        size=len(data),
        rows=len(rows),
        body=data,
        uploaded_at=at,
        uploaded_by=by,
    )
    session.add(row)
    session.flush()
    return row, True


def withdraw_drop(session: Session, drop_id: str, *, by: str, at: datetime) -> bool:
    """올린 판을 거둔다(소유자만). 다음 수집부터 그 앞의 판을 읽는다."""
    row = session.get(SourceDropRow, drop_id)
    if row is None or row.withdrawn_at is not None:
        return False
    _file_source(session, row.source_id, by)
    row.withdrawn_at = at
    return True


def read_current(session: Session, source_id: str) -> list[NoticeRecord]:
    """수집이 쓰는 것: 마지막 판의 줄을 NoticeRecord로. 판이 없으면 빈 목록. 바꾸지 못하는 줄은 건너뛴다."""
    row = session.get(UserSourceRow, source_id) if _ready(session) else None
    found = current_drop(session, source_id) if row is not None else None
    if found is None or row.removed_at is not None:
        return []
    sheet = SourceSheet.model_validate(row.body)
    convert = file_drop.to_record(source_id, sheet)
    out = []
    for r in file_drop.parse(found.body, sheet.connector.format):
        try:
            out.append(convert(r))
        except SourceError:
            continue
    return out


def forget(session: Session, person_id: str, *, at: datetime) -> int:
    """사람이 정보를 지울 때: 그 사람의 출처를 멈추고 올린 파일의 내용을 지운다(판의 기록은 남는다)."""
    if not _ready(session):
        return 0
    mine = session.scalars(
        sa.select(UserSourceRow).where(
            UserSourceRow.owner == person_id, UserSourceRow.removed_at.is_(None)
        )
    ).all()
    for r in mine:
        r.removed_at = at
    cleared = session.execute(
        sa.update(SourceDropRow)
        .where(SourceDropRow.uploaded_by == person_id, SourceDropRow.body.is_not(None))
        .values(body=None, withdrawn_at=sa.func.coalesce(SourceDropRow.withdrawn_at, at))
        .execution_options(synchronize_session=False)
    ).rowcount
    return len(mine) + cleared
