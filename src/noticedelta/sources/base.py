"""수집 결과의 공통 형태."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


class SourceError(Exception):
    """출처 API 호출 실패. 메시지에 인증키나 요청 URL을 넣지 않는다."""


@dataclass(frozen=True)
class AttachmentRef:
    filename: str
    url: str


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
        """원본 응답 항목의 해시. 행 번호·조회수처럼 내용과 무관한 값은 빼고 계산한다."""
        raw = {k: v for k, v in self.raw.items() if k not in {"id", "totCnt", "inqireCo"}}
        canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
