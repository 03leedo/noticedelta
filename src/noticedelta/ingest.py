"""수집 결과를 DB와 원문 저장소에 기록한다 (설계 7.1 → 7.2).

새 버전이 생겼을 때만 첨부를 내려받는다. 첨부 하나가 실패해도 나머지는 계속 받고,
실패한 첨부는 blob_key가 비어 있는 채로 남아 다음 수집 때 다시 시도된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from noticedelta.blobstore import BlobStore
from noticedelta.db import repo
from noticedelta.db.models import Notice, NoticeVersion
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
                AttachmentRef(att.filename, att.url),
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
