"""K-Startup 사업공고 API (data.go.kr 15125364, 창업진흥원).

명세: https://www.data.go.kr/data/15125364/openapi.do (Swagger, 2026-10-16 확인)
- 응답 data는 명세상 data.data[]지만 2026-10-19 실제 호출에서는 data[]로 왔다. 둘 다 받는다.
- pbanc_sn은 정수, 업력(biz_enyy)·연령(biz_trgt_age)·신청 대상(aply_trgt)은 쉼표로 이은 분류 목록이다.
- 날짜는 yyyyMMdd 문자열, 빈 값은 "".
- 첨부파일 URL 필드는 없다. 첨부는 기업마당 API에서 받는다 (docs/research/terms.md).
"""

from __future__ import annotations

import httpx

from noticedelta.sources.base import NoticeRecord, normalize_service_key
from noticedelta.sources.http import get_json
from noticedelta.sources.resilience import CircuitBreaker, RateLimiter

ENDPOINT = "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01"

_BODY_FIELDS = [
    ("사업 개요", "pbanc_ctnt"),
    ("신청 대상", "aply_trgt_ctnt"),
    ("제외 대상", "aply_excl_trgt_ctnt"),
    ("업력", "biz_enyy"),
    ("대상 연령", "biz_trgt_age"),
    ("지역", "supt_regin"),
    ("우대 사항", "prfn_matr"),
    ("신청 대상 구분", "aply_trgt"),
    ("지원 분야", "supt_biz_clsfc"),
    ("담당 부서", "biz_prch_dprt_nm"),
    ("문의처", "prch_cnpl_no"),
]


def _ymd(v) -> str | None:
    s = str(v or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def _items(payload: dict) -> list[dict]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = data.get("data") or []
    return list(data)


def to_record(item: dict) -> NoticeRecord:
    blocks = [
        f"{label}: {str(item[k]).strip()}"
        for label, k in _BODY_FIELDS
        if str(item.get(k) or "").strip()
    ]
    start, end = _ymd(item.get("pbanc_rcpt_bgng_dt")), _ymd(item.get("pbanc_rcpt_end_dt"))
    if start or end:
        blocks.append(f"접수 기간: {start or ''} ~ {end or ''}")
    return NoticeRecord(
        source="kstartup",
        external_id=str(item["pbanc_sn"]),
        title=str(item.get("biz_pbanc_nm") or "").strip(),
        org=item.get("pbanc_ntrp_nm") or None,
        url=item.get("detl_pg_url") or None,
        body="\n\n".join(blocks),
        apply_start=start,
        apply_end=end,
        attachments=[],
        raw=item,
    )


class KStartupClient:
    def __init__(
        self,
        service_key: str,
        http: httpx.Client,
        *,
        breaker: CircuitBreaker | None = None,
        limiter: RateLimiter | None = None,
        endpoint: str = ENDPOINT,
    ):
        self._key = normalize_service_key(service_key)
        self.http = http
        self.breaker = breaker
        self.limiter = limiter
        self.endpoint = endpoint

    def list_notices(
        self, page: int = 1, per_page: int = 100, *, only_open: bool = False
    ) -> list[NoticeRecord]:
        params = {"serviceKey": self._key, "page": page, "perPage": per_page, "returnType": "json"}
        if only_open:
            params["cond[rcrt_prgs_yn::EQ]"] = "Y"
        payload = get_json(
            self.http,
            self.endpoint,
            params,
            source="kstartup",
            breaker=self.breaker,
            limiter=self.limiter,
        )
        return [to_record(it) for it in _items(payload) if it.get("pbanc_sn") is not None]
