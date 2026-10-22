"""첨부 내려받기. 받은 원본은 내용 해시 키로 원문 저장소에 둔다 (설계 7.1, 7.2).

- 크기 상한을 넘으면 받다가 멈춘다(기본 50MB). 전체 시간 상한도 둔다(기본 300초).
- https와 허용한 호스트만 받고, 리다이렉트는 직접 따라가며 매번 다시 확인한다.
- 형식은 확장자가 아니라 앞부분 바이트로 판별한다. 파서가 형식을 믿고 열기 때문이다.
- 첨부는 재배포하지 않는다(공공누리 3유형). 저장소는 처리용이다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from noticedelta.blobstore import BlobStore
from noticedelta.sources.base import AttachmentRef, SourceError
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter

MAX_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 3
# 첨부 URL은 기업마당 API가 준다(K-Startup은 첨부를 주지 않는다). 다른 출처를 붙이면 여기에 더한다.
ALLOWED_HOSTS = frozenset({"www.bizinfo.go.kr", "bizinfo.go.kr"})

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


def check_url(url: str, allowed_hosts: frozenset[str]) -> None:
    """첨부는 https로, 허용한 출처 호스트에서만 받는다 (사설 주소·메타데이터 주소로 새지 않게)."""
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise SourceError("첨부 주소 형식이 잘못됨") from None
    if u.scheme != "https" or u.host not in allowed_hosts:
        raise SourceError(f"허용하지 않은 첨부 주소: {u.scheme}://{u.host}")


def _fetch(
    http: httpx.Client,
    url: str,
    source: str,
    allowed_hosts: frozenset[str],
    max_bytes: int,
    total_timeout_s: float,
) -> bytes:
    deadline = time.monotonic() + total_timeout_s
    for _ in range(MAX_REDIRECTS + 1):
        try:
            with http.stream("GET", url, timeout=60, follow_redirects=False) as resp:
                if resp.is_redirect:
                    url = str(resp.url.join(resp.headers.get("location", "")))
                    check_url(url, allowed_hosts)  # 리다이렉트마다 다시 확인한다
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SourceError(f"{source} 첨부가 너무 큼: {max_bytes} 바이트 초과")
                    if time.monotonic() > deadline:
                        raise SourceError(f"{source} 첨부 받기가 {total_timeout_s:.0f}초를 넘김")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPStatusError as e:
            raise SourceError(f"{source} 첨부 오류: HTTP {e.response.status_code}") from None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            raise SourceError(f"{source} 첨부 받기 실패: {type(e).__name__}") from None
    raise SourceError(f"{source} 첨부 리다이렉트가 {MAX_REDIRECTS}번을 넘음")


def download_attachment(
    http: httpx.Client,
    ref: AttachmentRef,
    store: BlobStore,
    *,
    source: str,
    breaker: CircuitBreaker | None = None,
    limiter: RateLimiter | None = None,
    max_bytes: int = MAX_BYTES,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    total_timeout_s: float = 300,
) -> StoredAttachment:
    check_url(ref.url, allowed_hosts)
    if breaker is not None and not breaker.allow():
        raise CircuitOpen(source)
    if limiter is not None:
        limiter.acquire()
    try:
        data = _fetch(http, ref.url, source, allowed_hosts, max_bytes, total_timeout_s)
        mime = sniff_mime(data, ref.filename)
        if mime == "text/html":
            # 첨부 대신 오류·로그인 페이지가 온 경우. 저장하지 않고 출처 실패로 센다.
            raise SourceError(f"{source} 첨부 대신 HTML 응답")
    except BaseException:
        # 어떤 실패든 결과를 기록해야 half-open 시험 칸이 풀린다.
        if breaker is not None:
            breaker.record_failure()
        raise
    if breaker is not None:
        breaker.record_success()
    blob = store.put(data, content_type=mime)
    return StoredAttachment(ref.filename, ref.url, blob.key, blob.size, mime)
