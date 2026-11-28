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
from noticedelta.db.models import Attachment, Notice, NoticeVersion, ParseCache
from noticedelta.parsing import (
    PARSEABLE_MIMES,
    ParsedDocument,
    Parser,
    review_reason,
    to_blocks,
)
from noticedelta.segments import Segment, segment_document
from noticedelta.sources.attachments import StoredAttachment, download_attachment
from noticedelta.sources.base import AttachmentRef, NoticeRecord, SourceError
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
    # 내용이 같아도 지난번에 받지 못한 첨부(일시 장애, 서킷 열림)는 현재 버전 기준으로 다시 받는다.
    target = version or repo.latest_version(session, notice.id)
    if target is None:
        return result
    for att in repo.attachments_to_fetch(session, target.id):
        try:
            stored = download_attachment(
                http,
                AttachmentRef(att.filename, att.url, att.role),
                store,
                source=rec.source,
                breaker=breaker,
                limiter=limiter,
                allowed_hosts=rec.attachment_hosts,
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
    session.flush()
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
        cached = session.get(ParseCache, (att.blob_key, att.mime))
        if cached is not None:
            # 같은 파일은 다시 파싱하지 않는다(재게시, 주소만 바뀐 첨부, DB를 다시 만들 때)
            att.parsed_key, att.parser = cached.parsed_key, cached.parser
            att.parse_status, att.review_reason = cached.parse_status, cached.review_reason
            outcomes.append(
                ParseOutcome(
                    att.filename, att.parse_status, att.parser, cached.segments, att.review_reason
                )
            )
            continue
        # ParserUnavailable(Docker 꺼짐 등)은 그대로 올려 보낸다. 남은 첨부는 pending으로 남는다.
        doc = parser.parse(store.get(att.blob_key), att.mime)
        # ensure_ascii: 깨진 HWP에서 나온 짝 없는 서로게이트도 인코딩 오류 없이 저장한다.
        payload = json.dumps(doc.to_json(), ensure_ascii=True, sort_keys=True).encode("ascii")
        att.parsed_key = store.put(payload, content_type="application/json").key
        att.parser = f"{doc.parser} {doc.parser_version}".strip()
        reason = review_reason(doc)
        att.parse_status = "needs_review" if reason else "ok"
        att.review_reason = reason
        n = 0 if reason else len(segment_document(to_blocks(doc), source=_source_id(att)))
        outcomes.append(ParseOutcome(att.filename, att.parse_status, att.parser, n, reason))
        if doc.status == "ok":
            # 시간 초과 같은 실패는 남기지 않는다. 다음 버전에서 다시 파싱해 볼 수 있게.
            session.add(
                ParseCache(
                    blob_key=att.blob_key,
                    mime=att.mime,
                    parse_status=att.parse_status,
                    parser=att.parser,
                    parsed_key=att.parsed_key,
                    review_reason=reason,
                    segments=n,
                )
            )
            session.flush()
    session.flush()
    return outcomes


def _attachment_order(att: Attachment) -> tuple:
    return (att.role != "notice", att.filename)  # 공고문 본 파일 먼저


def _source_id(att: Attachment) -> str:
    return f"att:{att.blob_key[:12]}"


def body_segments(body: str, body_keys: list | None) -> list[Segment]:
    """본문의 조각. 조각마다 원본 키(locator.keys)를 적는다(app-spec 9장: 앱은 읽는 키의 조각만 본다).

    조각은 본문 전체를 한 번에 나눈 것 그대로다(키를 적는다고 조각이 달라지지 않는다). 부분마다 따로
    나눈 결과가 전체를 나눈 결과와 글자까지 같을 때만 키를 적고, 아니면(예전 판, 부분 경계를 넘는 조각)
    적지 않는다. 키가 없는 조각은 그 출처를 읽는 모든 앱이 본다.
    """
    segments = segment_document([body], source="body") if body else []
    keys = _part_keys(body, body_keys)
    if keys is None:
        return segments
    flat: list[tuple[str, str]] = []
    owners: list[list[str]] = []
    for part_keys, text in keys:
        for s in segment_document([text], source="body"):
            flat.append((s.kind, s.text))
            owners.append(sorted(part_keys))
    if flat != [(s.kind, s.text) for s in segments]:
        return segments
    return [
        replace(s, locator={**s.locator, "keys": k}) for s, k in zip(segments, owners, strict=True)
    ]


def _part_keys(body: str, body_keys: list | None) -> list[tuple[list[str], str]] | None:
    """[[키들, 글자 수], …]를 본문에서 잘라 (키들, 글)로. 빈 줄 하나를 두고 이은 것과 맞지 않으면 None."""
    if not body or not body_keys:
        return None
    out, pos = [], 0
    for i, (keys, n) in enumerate(body_keys):
        if i and body[pos : pos + 2] != "\n\n":
            return None
        pos += 2 if i else 0
        out.append((list(keys), body[pos : pos + n]))
        pos += n
    return out if pos == len(body) else None


def version_segments(session: Session, version: NoticeVersion, store: BlobStore) -> list[Segment]:
    """버전 하나의 조각 전체: API 본문, 그다음 파싱에 성공한 첨부들(공고문 본 파일 먼저).

    조각 위치(locator)의 source는 "body" 또는 "att:<원문 해시 앞 12자>"이고, 첨부 조각에는
    파일 이름·역할을 더한다. 파일 이름이 바뀌어도 내용이 같으면 같은 source다. 본문 조각에는 원본 키를
    더한다(keys, 알 수 있을 때만. body_segments).
    """
    segments = body_segments(version.body, version.body_keys)
    rows = session.scalars(
        sa.select(Attachment).where(
            Attachment.notice_version_id == version.id, Attachment.parse_status == "ok"
        )
    ).all()
    for att in sorted(rows, key=_attachment_order):
        doc = ParsedDocument.from_json(json.loads(store.get(att.parsed_key)))
        segs = segment_document(to_blocks(doc), source=_source_id(att), start_order=len(segments))
        segments += [
            replace(
                s,
                locator={
                    **s.locator,
                    "filename": att.filename,
                    "role": att.role,
                    "attachment": att.lineage_key,
                },
            )
            for s in segs
        ]
    return segments
