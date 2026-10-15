"""출처 API 공통 GET. 실패 메시지에서 URL(인증키 포함)을 숨긴다."""

from __future__ import annotations

import httpx

from noticedelta.sources.base import SourceError
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter


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
        raise SourceError(f"{source} API 오류: HTTP {e.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as e:
        if breaker is not None:
            breaker.record_failure()
        raise SourceError(f"{source} API 호출 실패: {type(e).__name__}") from None
    if breaker is not None:
        breaker.record_success()
    return data
