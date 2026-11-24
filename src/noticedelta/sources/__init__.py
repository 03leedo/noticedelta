from noticedelta.sources import bizinfo, kstartup
from noticedelta.sources.base import (
    AttachmentRef,
    NoticeRecord,
    SourceError,
    TransientSourceError,
)
from noticedelta.sources.bizinfo import BizinfoClient
from noticedelta.sources.kstartup import KStartupClient
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter

# 운영자 출처의 카탈로그 (source-catalog.md). 앱 정의의 검증이 여기서 키와 구조화 값을 받는다
CATALOG = {s.id: s for s in (bizinfo.SHEET, kstartup.SHEET)}

__all__ = [
    "CATALOG",
    "AttachmentRef",
    "BizinfoClient",
    "CircuitBreaker",
    "CircuitOpen",
    "KStartupClient",
    "NoticeRecord",
    "RateLimiter",
    "SourceError",
    "TransientSourceError",
]
