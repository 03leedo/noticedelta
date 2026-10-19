"""수집 결과를 DB와 원문 저장소에 기록하고 첨부를 파싱한다 (설계 7.1 → 7.2 → 7.3).

새 버전이 생겼을 때만 첨부를 내려받는다. 첨부 하나가 실패해도 나머지는 계속 받고,
실패한 첨부는 blob_key가 비어 있는 채로 남아 다음 수집 때 다시 시도된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.blobstore import BlobStore
from noticedelta.db import repo
from noticedelta.db.models import Attachment, Notice, NoticeVersion
from noticedelta.parsing import (
    PARSEABLE_MIMES,
    ParsedDocument,
    Parser,
    review_reason,
    to_blocks,
)
from noticedelta.segments import Segment, segment_document
from noticedelta.sources import AttachmentRef, NoticeRecord, SourceError
from noticedelta.sources.attachments import StoredAttachment, download_attachment
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter


@dataclass
class IngestResult:
    notice: Notice
    version: NoticeVersion | None  # None이면 내용이 바뀌지 않음
    stored: list[StoredAttachment] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (파일명, 사유)


def ingest_record(
    session: Session,
    rec: NoticeRecord,
    *,
    http: httpx.Client,
    store: BlobStore,
    breaker: CircuitBreaker | None = None,
    limiter: RateLimiter | None = None,
) -> IngestResult:
    notice, version = repo.upsert_notice_version(session, rec)
    result = IngestResult(notice, version)
    if version is None:
        return result
    for att in repo.attachments_to_fetch(session, version.id):
        try:
            stored = download_attachment(
                http,
                AttachmentRef(att.filename, att.url, att.role),
                store,
                source=rec.source,
                breaker=breaker,
                limiter=limiter,
            )
        except CircuitOpen:
            result.failed.append((att.filename, "서킷 열림"))
            break  # 출처가 쉬는 중이면 남은 첨부도 다음 수집으로 미룬다
        except SourceError as e:
            result.failed.append((att.filename, str(e)))
            continue
        att.blob_key, att.mime = stored.blob_key, stored.mime
        result.stored.append(stored)
    session.flush()
    return result


@dataclass
class ParseOutcome:
    filename: str
    status: str  # ok / needs_review / unsupported
    parser: str | None = None
    segments: int = 0
    reason: str | None = None


def parse_attachments(
    session: Session, version_id: str, *, parser: Parser, store: BlobStore
) -> list[ParseOutcome]:
    """저장된 첨부 중 아직 파싱하지 않은 것을 격리 파서로 파싱한다 (설계 7.3).

    파서 출력 JSON은 원문 저장소에 내용 해시 키로 두고 attachment.parsed_key에 기록한다.
    같은 출력은 한 번만 저장되고, 조각은 언제든 이 JSON에서 다시 만들 수 있다.
    """
    outcomes = []
    rows = session.scalars(
        sa.select(Attachment).where(
            Attachment.notice_version_id == version_id,
            Attachment.blob_key.is_not(None),
            Attachment.parse_status == "pending",
        )
    ).all()
    for att in sorted(rows, key=_attachment_order):
        if att.mime not in PARSEABLE_MIMES:
            att.parse_status = "unsupported"
            att.review_reason = f"파싱하지 않는 형식: {att.mime}"
            outcomes.append(ParseOutcome(att.filename, att.parse_status, reason=att.review_reason))
            continue
        doc = parser.parse(store.get(att.blob_key), att.mime)
        payload = json.dumps(doc.to_json(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        att.parsed_key = store.put(payload, content_type="application/json").key
        att.parser = f"{doc.parser} {doc.parser_version}".strip()
        reason = review_reason(doc)
        att.parse_status = "needs_review" if reason else "ok"
        att.review_reason = reason
        n = 0 if reason else len(segment_document(to_blocks(doc), source=_source_id(att)))
        outcomes.append(ParseOutcome(att.filename, att.parse_status, att.parser, n, reason))
    session.flush()
    return outcomes


def _attachment_order(att: Attachment) -> tuple:
    return (att.role != "notice", att.filename)  # 공고문 본 파일 먼저


def _source_id(att: Attachment) -> str:
    return f"att:{att.blob_key[:12]}"


def version_segments(session: Session, version: NoticeVersion, store: BlobStore) -> list[Segment]:
    """버전 하나의 조각 전체: API 본문, 그다음 파싱에 성공한 첨부들(공고문 본 파일 먼저).

    조각 위치(locator)의 source는 "body" 또는 "att:<원문 해시 앞 12자>"이고, 첨부 조각에는
    파일 이름·역할을 더한다. 파일 이름이 바뀌어도 내용이 같으면 같은 source다.
    """
    segments = segment_document([version.body], source="body") if version.body else []
    rows = session.scalars(
        sa.select(Attachment).where(
            Attachment.notice_version_id == version.id, Attachment.parse_status == "ok"
        )
    ).all()
    for att in sorted(rows, key=_attachment_order):
        doc = ParsedDocument.from_json(json.loads(store.get(att.parsed_key)))
        segs = segment_document(to_blocks(doc), source=_source_id(att), start_order=len(segments))
        segments += [
            replace(s, locator={**s.locator, "filename": att.filename, "role": att.role})
            for s in segs
        ]
    return segments
