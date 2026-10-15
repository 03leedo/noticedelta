"""기업마당 지원사업정보 API.

- data.go.kr 판(15157820, 중소벤처기업부): https://www.data.go.kr/data/15157820/openapi.do
  공공누리 제3유형(출처표시+변경금지). 이 프로젝트의 기본 수집원이다 (docs/research/terms.md).
- 기업마당 자체 API: https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi (crtfcKey)

두 API의 item 필드는 거의 같고 응답 껍데기만 다르다. 실제 응답이 명세와 다른 경우가 있어
jsonArray[], jsonArray.item(배열·객체·null), response.body.items.item을 모두 받는다.
첨부는 fileNm / flpthNm에 @로 이어 붙어 오고 같은 순서끼리 짝이다.
"""

from __future__ import annotations

import html
import re

import httpx

from noticedelta.sources.base import AttachmentRef, NoticeRecord
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
    names = (item.get("fileNm") or "").split("@")
    urls = (item.get("flpthNm") or "").split("@")
    return [
        AttachmentRef(n.strip(), u.strip())
        for n, u in zip(names, urls, strict=False)
        if n.strip() and u.strip()
    ]


def _items(payload: dict) -> list[dict]:
    if "response" in payload:
        body = (payload.get("response") or {}).get("body") or {}
        items = (body.get("items") or {}).get("item")
    else:
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
        self._key = key
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
        return [to_record(it) for it in _items(payload) if it.get("pblancId")]
