"""기업마당 지원사업정보 API.

- data.go.kr 판(15157820, 중소벤처기업부): https://www.data.go.kr/data/15157820/openapi.do
  공공누리 제3유형(출처표시+변경금지). 이 프로젝트의 기본 수집원이다 (docs/research/terms.md).
  응답 형태는 2026-10-19 실제 호출로 확인했다 (docs/research/api-responses.md):
  response.header.resultCode == "00", 항목은 response.body.items.item 배열(한 건이어도 배열),
  빈 페이지는 items가 {}.
- 기업마당 자체 API: https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi (crtfcKey)
  응답(jsonArray)은 아직 실제로 확인하지 못했다.

첨부는 두 갈래로 온다. printFileNm/printFlpthNm은 공고문 본 파일 하나, fileNm/flpthNm은
붙임 서식들이 @로 이어 붙어 오고 같은 순서끼리 짝이다.
"""

from __future__ import annotations

import html
import re

import httpx

from noticedelta.sources.base import (
    AttachmentRef,
    NoticeRecord,
    SourceError,
    normalize_service_key,
)
from noticedelta.sources.http import get_json
from noticedelta.sources.resilience import CircuitBreaker, RateLimiter

DATA_GO_KR_ENDPOINT = "https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService"
BIZINFO_ENDPOINT = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</p>|<br\s*/?>|</li>|</div>", re.IGNORECASE)
_DATE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


def html_to_text(s: str) -> str:
    s = _BLOCK_END.sub("\n\n", s or "")
    s = html.unescape(_TAG.sub("", s)).replace("\xa0", " ")
    return "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", s) if p.strip())


def _period(v: str | None) -> tuple[str | None, str | None]:
    parts = (v or "").split("~")
    if len(parts) != 2:
        return None, None
    out = []
    for p in parts:
        m = _DATE.search(p)
        out.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None)
    return out[0], out[1]


def _attachments(item: dict) -> list[AttachmentRef]:
    out = []
    name, url = (item.get("printFileNm") or "").strip(), (item.get("printFlpthNm") or "").strip()
    if name and url:
        out.append(AttachmentRef(name, url, role="notice"))
    names = (item.get("fileNm") or "").split("@")
    urls = (item.get("flpthNm") or "").split("@")
    out += [
        AttachmentRef(n.strip(), u.strip())
        for n, u in zip(names, urls, strict=False)
        if n.strip() and u.strip()
    ]
    return out


def _items_data_go_kr(payload: dict) -> list[dict]:
    resp = payload.get("response")
    if not isinstance(resp, dict):
        # 형태가 바뀌었는데 빈 목록으로 넘기면 수집이 조용히 멈춘다. 오류로 드러낸다.
        raise SourceError("bizinfo API 응답 형태가 예상과 다름: response 없음")
    header = resp.get("header") or {}
    if header.get("resultCode", "00") != "00":
        raise SourceError(
            f"bizinfo API 오류: {header.get('resultCode')} {header.get('resultMsg', '')}".strip()
        )
    items = ((resp.get("body") or {}).get("items") or {}).get("item") or []
    return list(items)


def _items_bizinfo(payload: dict) -> list[dict]:
    """기업마당 자체 API (미확인). 명세의 jsonArray[]와 jsonArray.item 둘 다 받는다."""
    arr = payload.get("jsonArray")
    items = arr.get("item") if isinstance(arr, dict) else arr
    if items is None:
        return []
    return [items] if isinstance(items, dict) else list(items)


def to_record(item: dict) -> NoticeRecord:
    start, end = _period(item.get("reqstBeginEndDe"))
    blocks = [html_to_text(item.get("bsnsSumryCn") or "")]
    for label, k in [
        ("지원 대상", "trgetNm"),
        ("신청 기간", "reqstBeginEndDe"),
        ("제출 서류", "reqstMthPapersCn"),
        ("문의처", "refrncNm"),
    ]:
        if str(item.get(k) or "").strip():
            blocks.append(f"{label}: {html_to_text(str(item[k]))}")
    return NoticeRecord(
        source="bizinfo",
        external_id=str(item["pblancId"]),
        title=str(item.get("pblancNm") or "").strip(),
        org=item.get("excInsttNm") or item.get("jrsdInsttNm") or None,
        url=item.get("pblancUrl") or None,
        body="\n\n".join(b for b in blocks if b),
        apply_start=start,
        apply_end=end,
        attachments=_attachments(item),
        raw=item,
    )


class BizinfoClient:
    """기본은 data.go.kr 판. 기업마당 자체 API를 쓰려면 api="bizinfo"."""

    def __init__(
        self,
        key: str,
        http: httpx.Client,
        *,
        api: str = "data_go_kr",
        breaker: CircuitBreaker | None = None,
        limiter: RateLimiter | None = None,
    ):
        self._key = normalize_service_key(key)
        self.http = http
        self.api = api
        self.breaker = breaker
        self.limiter = limiter

    def list_notices(self, page: int = 1, per_page: int = 100) -> list[NoticeRecord]:
        if self.api == "bizinfo":
            url = BIZINFO_ENDPOINT
            params = {
                "crtfcKey": self._key,
                "dataType": "json",
                "pageIndex": page,
                "pageUnit": per_page,
            }
        else:
            url = DATA_GO_KR_ENDPOINT
            params = {
                "serviceKey": self._key,
                "dataType": "json",
                "pageNo": page,
                "numOfRows": per_page,
            }
        payload = get_json(
            self.http, url, params, source="bizinfo", breaker=self.breaker, limiter=self.limiter
        )
        items = _items_bizinfo(payload) if self.api == "bizinfo" else _items_data_go_kr(payload)
        return [to_record(it) for it in items if it.get("pblancId")]
