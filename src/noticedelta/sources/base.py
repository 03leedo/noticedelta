"""수집 결과의 공통 형태."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import unquote

_VOLATILE = {"id", "totCnt", "inqireCo", "updtPnttm"}
# 기업마당 첨부 주소. 파일이 그대로여도 주소를 한꺼번에 새로 발급한다(2026-10-30, 1,491건 전부).
# 게시물 버전은 주소가 바뀌면 새로 만들지만(첨부를 새 주소로 다시 받아야 하므로),
# 사업 버전은 받은 첨부의 내용 해시로 비교하므로 주소를 빼고 본다.
_ATTACHMENT_URLS = {"flpthNm", "printFlpthNm"}


def _raw_hash(raw: dict, skip: set[str]) -> str:
    kept = {k: v for k, v in raw.items() if k not in skip}
    canonical = json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def body_hash(raw: dict) -> str:
    """첨부 주소까지 뺀 원본 항목의 해시. 사업 버전 비교에 쓴다(programs.composite_hash)."""
    return _raw_hash(raw, _VOLATILE | _ATTACHMENT_URLS)


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
