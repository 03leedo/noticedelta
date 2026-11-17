"""수집 결과의 공통 형태."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import unquote

from noticedelta.text import canonical_text

_VOLATILE = {"id", "totCnt", "inqireCo", "updtPnttm"}
# 기업마당 첨부 주소. 파일이 그대로여도 주소를 한꺼번에 새로 발급한다(2026-10-30, 1,491건 전부).
# 게시물 버전은 주소가 바뀌면 새로 만들지만(첨부를 새 주소로 다시 받아야 하므로),
# 사업 버전은 받은 첨부의 내용 해시로 비교하므로 주소를 빼고 본다.
_ATTACHMENT_URLS = {"flpthNm", "printFlpthNm"}
# 호출할 때마다 새로 발급되는 URL 쿼리 값. 주소를 비교·저장할 때는 뺀다.
# downloadToken: 기업마당이 2026-10-30부터 첨부 주소에 붙이는 내려받기 토큰(JWT, 발급 시각이 들어 있다).
_VOLATILE_QUERY = {"downloadToken"}


def stable_url(url: str) -> str:
    """호출마다 바뀌는 쿼리 값을 뺀 주소. 같은 파일이면 같은 문자열이다.

    비교·저장에만 쓴다. 실제로 받을 때는 토큰이 붙은 원래 주소를 쓴다.
    다시 인코딩하지 않고 문자열에서 그 값만 잘라 내, 토큰이 없던 때의 주소와 똑같아지게 한다.
    """
    head, sep, rest = url.partition("?")
    if not sep:
        return url
    query, hash_sep, fragment = rest.partition("#")
    kept = [p for p in query.split("&") if p.split("=", 1)[0] not in _VOLATILE_QUERY]
    return head + ("?" + "&".join(kept) if kept else "") + hash_sep + fragment


def _raw_hash(raw: dict, skip: set[str], *, canonical: bool = False) -> str:
    kept = {k: v for k, v in raw.items() if k not in skip}
    if canonical:
        # 표기만 다른 글을 같은 것으로, 빈 값과 없는 키를 같은 것으로 본다
        kept = {k: canonical_text(v) if isinstance(v, str) else v for k, v in kept.items()}
        kept = {k: v for k, v in kept.items() if v not in (None, "")}
    canonical = json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def body_hash(raw: dict) -> str:
    """첨부 주소를 빼고 표기 차이를 지운 원본 항목의 해시. 사업 버전 비교에 쓴다(programs.composite_hash).

    게시물 버전의 해시(content_hash)는 원본 그대로 계산한다. 원본이 글자 하나라도 다르면 게시물
    버전으로 남기고, 내용이 같으면 사업 버전은 만들지 않는다.
    """
    return _raw_hash(raw, _VOLATILE | _ATTACHMENT_URLS, canonical=True)


def normalize_service_key(key: str) -> str:
    """data.go.kr는 같은 키를 Encoding판(%2B 등)과 Decoding판으로 준다.

    httpx가 쿼리를 다시 인코딩하므로 Encoding판을 그대로 쓰면 두 번 인코딩돼 인증에 실패한다.
    퍼센트 인코딩이 보이면 풀어서 Decoding판으로 쓴다.
    """
    key = key.strip()
    return unquote(key) if "%" in key else key


class SourceError(Exception):
    """출처 API 호출 실패. 메시지에 인증키나 요청 URL을 넣지 않는다."""


class TransientSourceError(SourceError):
    """잠시 뒤 다시 하면 될 수 있는 실패: 연결 실패·시간 초과, HTTP 429·5xx."""


@dataclass(frozen=True)
class AttachmentRef:
    filename: str
    url: str
    role: str = "attachment"  # notice(공고문 본 파일) / attachment(붙임 서식 등)

    @property
    def key(self) -> str:
        """이 첨부를 가리키는 변하지 않는 주소 (수집 기록·DB에 남기는 값)."""
        return stable_url(self.url)


@dataclass
class NoticeRecord:
    source: str
    external_id: str
    title: str
    org: str | None
    url: str | None
    body: str
    apply_start: str | None  # YYYY-MM-DD
    apply_end: str | None  # YYYY-MM-DD
    attachments: list[AttachmentRef] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """원본 응답 항목의 해시. 내용과 무관한 값은 빼고 계산한다.

        - id(K-Startup 행 번호), totCnt, inqireCo(조회수)
        - updtPnttm(기업마당 수정 시각): 여러 공고가 같은 초에 함께 바뀌는 일괄 갱신이 있어
          내용이 그대로여도 값이 바뀐다 (docs/research/api-responses.md).
        """
        return _raw_hash(self.raw, _VOLATILE)
