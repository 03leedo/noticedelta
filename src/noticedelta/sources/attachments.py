"""첨부 내려받기. 받은 원본은 내용 해시 키로 원문 저장소에 둔다 (설계 7.1, 7.2).

- 크기 상한을 넘으면 받다가 멈춘다(기본 50MB).
- 형식은 확장자가 아니라 앞부분 바이트로 판별한다. 파서가 형식을 믿고 열기 때문이다.
- 첨부는 재배포하지 않는다(공공누리 3유형). 저장소는 처리용이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from noticedelta.blobstore import BlobStore
from noticedelta.sources.base import AttachmentRef, SourceError
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter

MAX_BYTES = 50 * 1024 * 1024

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # HWP 5.x, 옛 MS Office
_HWP5_SIGNATURE = b"HWP Document File"


@dataclass(frozen=True)
class StoredAttachment:
    filename: str
    url: str
    blob_key: str
    size: int
    mime: str


def sniff_mime(data: bytes, filename: str = "") -> str:
    name = filename.lower()
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(_OLE):
        # HWP5는 FileHeader 스트림이 "HWP Document File"로 시작한다. 스트림 위치는 파일마다
        # 달라(실제 첨부에서 6KB~30MB 지점) 전체에서 찾는다. 확장자가 .hwpx인 HWP5도 있었다.
        if _HWP5_SIGNATURE in data or name.endswith(".hwp"):
            return "application/x-hwp"
        return "application/x-ole-storage"
    if data.startswith(b"PK\x03\x04"):
        # HWPX는 zip 안의 첫 항목이 mimetype이고 내용이 application/hwp+zip이다.
        if b"application/hwp+zip" in data[:200] or name.endswith(".hwpx"):
            return "application/hwp+zip"
        return "application/zip"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return "application/octet-stream"


def download_attachment(
    http: httpx.Client,
    ref: AttachmentRef,
    store: BlobStore,
    *,
    source: str,
    breaker: CircuitBreaker | None = None,
    limiter: RateLimiter | None = None,
    max_bytes: int = MAX_BYTES,
) -> StoredAttachment:
    if breaker is not None and not breaker.allow():
        raise CircuitOpen(source)
    if limiter is not None:
        limiter.acquire()
    try:
        with http.stream("GET", ref.url, timeout=60, follow_redirects=True) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise SourceError(f"{source} 첨부가 너무 큼: {max_bytes} 바이트 초과")
                chunks.append(chunk)
    except httpx.HTTPStatusError as e:
        if breaker is not None:
            breaker.record_failure()
        raise SourceError(f"{source} 첨부 오류: HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        if breaker is not None:
            breaker.record_failure()
        raise SourceError(f"{source} 첨부 받기 실패: {type(e).__name__}") from None
    if breaker is not None:
        breaker.record_success()

    data = b"".join(chunks)
    mime = sniff_mime(data, ref.filename)
    if mime == "text/html":
        # 첨부 대신 오류·로그인 페이지가 온 경우. 저장하지 않는다.
        raise SourceError(f"{source} 첨부 대신 HTML 응답")
    blob = store.put(data, content_type=mime)
    return StoredAttachment(ref.filename, ref.url, blob.key, blob.size, mime)
