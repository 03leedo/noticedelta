"""수집 결과의 공통 형태."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import unquote

from noticedelta.text import canonical_text


def body_parts(parts: Iterable[tuple[Iterable[str], str]]) -> tuple[str, tuple]:
    """(키들, 글) 목록에서 빈 글을 빼고 (본문, body_keys)를 만든다. 본문은 글을 빈 줄 둘로 잇는다."""
    kept = tuple((tuple(keys), text) for keys, text in parts if text)
    return "\n\n".join(text for _, text in kept), kept


def stable_url(url: str, drop: Iterable[str] = ()) -> str:
    """호출마다 바뀌는 쿼리 값(drop)을 뺀 주소. 같은 파일이면 같은 문자열이다.

    비교·저장에만 쓴다. 실제로 받을 때는 토큰이 붙은 원래 주소를 쓴다.
    다시 인코딩하지 않고 문자열에서 그 값만 잘라 내, 토큰이 없던 때의 주소와 똑같아지게 한다.
    """
    head, sep, rest = url.partition("?")
    if not sep:
        return url
    query, hash_sep, fragment = rest.partition("#")
    drop = set(drop)
    kept = [p for p in query.split("&") if p.split("=", 1)[0] not in drop]
    return head + ("?" + "&".join(kept) if kept else "") + hash_sep + fragment


def _raw_hash(raw: dict, skip: set[str], *, canonical: bool = False) -> str:
    kept = {k: v for k, v in raw.items() if k not in skip}
    if canonical:
        # 표기만 다른 글을 같은 것으로, 빈 값과 없는 키를 같은 것으로 본다
        kept = {k: canonical_text(v) if isinstance(v, str) else v for k, v in kept.items()}
        kept = {k: v for k, v in kept.items() if v not in (None, "")}
    canonical = json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    role: str = "attachment"  # notice(본 문서) / attachment(붙임 서식 등)
    drop_params: tuple[str, ...] = ()  # 주소에서 호출마다 바뀌는 쿼리 값 (출처가 정한다)

    @property
    def key(self) -> str:
        """이 첨부를 가리키는 변하지 않는 주소 (수집 기록·DB에 남기는 값)."""
        return stable_url(self.url, self.drop_params)


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
    # 아래는 출처가 정한다(어댑터의 to_record). 출처 카탈로그(source-catalog.md)의 코드판이다.
    ignore: frozenset[str] = frozenset()  # 바뀌어도 변경이 아닌 키(조회수, 일괄 수정 시각 등)
    url_keys: frozenset[str] = frozenset()  # 첨부 주소가 든 키. 항목 버전은 받은 파일로 비교한다
    attachment_hosts: frozenset[str] = frozenset()  # 첨부를 받아도 되는 호스트. 비면 받지 않는다
    # 본문(body)을 이룬 조각과 그 원본 키: ((키들, 글), …). 글을 빈 줄 하나를 두고 이으면 body다.
    # 조각마다 어느 키에서 왔는지 적어 앱이 자기가 읽는 키의 조각만 보게 한다(app-spec 9장). 비면 모른다
    body_keys: tuple[tuple[tuple[str, ...], str], ...] = ()

    @property
    def content_hash(self) -> str:
        """원본 응답 항목의 해시. 내용과 무관한 값(ignore)은 빼고 계산한다. 게시물 버전을 가른다."""
        return _raw_hash(self.raw, set(self.ignore))

    @property
    def body_hash(self) -> str:
        """첨부 주소까지 빼고 표기 차이를 지운 해시. 항목(사업) 버전 비교에 쓴다(programs.composite_hash).

        원본이 글자 하나라도 다르면 게시물 버전으로는 남기고, 내용이 같으면 항목 버전은 만들지 않는다.
        """
        return _raw_hash(self.raw, set(self.ignore | self.url_keys), canonical=True)
