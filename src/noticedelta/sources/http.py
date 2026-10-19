"""출처 API 공통 GET. 실패 메시지와 httpx 요청 로그에서 인증키를 숨긴다."""

from __future__ import annotations

import logging
import re

import httpx

from noticedelta.sources.base import SourceError
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter

_KEY_PARAM = re.compile(r"((?:serviceKey|crtfcKey)=)[^&\s\"]+", re.IGNORECASE)


class _RedactKeys(logging.Filter):
    """httpx는 INFO로 요청 URL 전체를 남긴다. 쿼리의 인증키를 가린다."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _KEY_PARAM.search(msg):
            record.msg, record.args = _KEY_PARAM.sub(r"\1***", msg), None
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactKeys) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactKeys())


def _openapi_error(resp: httpx.Response) -> str | None:
    """data.go.kr 공통 오류 응답의 오류 코드 (예: SERVICE_KEY_IS_NOT_REGISTERED_ERROR)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    header = (body.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader") or {}
    return header.get("errMsg") or None


def get_json(
    http: httpx.Client,
    url: str,
    params: dict,
    *,
    source: str,
    breaker: CircuitBreaker | None,
    limiter: RateLimiter | None,
) -> dict:
    if breaker is not None and not breaker.allow():
        raise CircuitOpen(source)
    if limiter is not None:
        limiter.acquire()
    try:
        resp = http.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        if breaker is not None:
            breaker.record_failure()
        err = _openapi_error(e.response)
        detail = f" ({err})" if err else ""
        raise SourceError(f"{source} API 오류: HTTP {e.response.status_code}{detail}") from None
    except (httpx.HTTPError, ValueError) as e:
        if breaker is not None:
            breaker.record_failure()
        raise SourceError(f"{source} API 호출 실패: {type(e).__name__}") from None
    if isinstance(data, dict) and "OpenAPI_ServiceResponse" in data:
        if breaker is not None:
            breaker.record_failure()
        raise SourceError(f"{source} API 오류: {_openapi_error(resp) or '알 수 없는 오류'}")
    if breaker is not None:
        breaker.record_success()
    return data
