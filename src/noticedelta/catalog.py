"""출처 한 장 (docs/source-catalog.md 2장). 출처마다 어디서, 어떻게 가져오고, 무엇을 무시하는지.

    load_sheet(path)          파일을 읽어 검증한다. 문제가 있으면 SheetError
    SourceSheet.model_json_schema()   에이전트가 읽는 JSON 스키마

앱 정의(appspec)는 출처를 고르고 그 출처가 내는 키(`keys`)와 구조화 값(`meta`)만 읽을 수 있다.
운영자 출처는 `connector.kind: code`(코드 어댑터)다. 사용자 출처는 `rss`와 `html_list`(게시판)를 받는다
(2026-11-27, 4장의 등록 흐름, user_sources.py). file_drop은 아직이다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from noticedelta.text import canonical_text

SourceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeConnector(_Strict):
    kind: Literal["code"]  # 운영자 출처: 코드 어댑터
    adapter: str


class RssConnector(_Strict):
    kind: Literal["rss"]  # 사용자 출처: RSS·Atom 피드
    url: str


class HtmlListConnector(_Strict):
    kind: Literal["html_list"]  # 사용자 출처: 게시판(글 목록 페이지)
    url: str
    list: str = Field(min_length=1, max_length=500)  # 글 링크까지의 조상 모양(플랫폼이 제안한 후보)


Connector = Annotated[CodeConnector | RssConnector | HtmlListConnector, Field(discriminator="kind")]


class Fetch(_Strict):
    every_hours: int = Field(ge=1, le=168)
    rate_per_second: float = Field(gt=0, le=10)


class ItemKeys(_Strict):
    """항목을 가리키는 키: 같은 항목인지(id), 사람에게 보일 제목과 주소."""

    id: str
    title: str
    url: str | None = None


class KeyInfo(_Strict):
    about: str  # 에이전트가 읽고 고른다. 코드를 읽지 않아도 되게
    html: bool = False  # 값에 HTML이 들어 있다


class MetaInfo(_Strict):
    """출처가 구조화해 준 값. 앱의 `from: {meta: 이름}`이 가리킨다."""

    key: str
    about: str


class Ignore(_Strict):
    keys: list[str] = []  # 바뀌어도 변경이 아닌 키
    url_params: list[str] = []  # 주소에서 떼는 값(호출마다 바뀌는 토큰)
    url_keys: list[str] = []  # 파일이 그대로여도 주소를 새로 발급하는 키: 주소 대신 내용으로 비교


class Attachments(_Strict):
    names: list[str]
    urls: list[str]
    hosts: list[str] = Field(min_length=1)  # 받아도 되는 호스트. 그 밖은 막는다(6장)


class Terms(_Strict):
    license: str
    quote_limit: int = Field(ge=0, le=5000)  # 원문을 한 번에 보여 줄 수 있는 글자 수
    note: str | None = None
    agreed_by: str | None = None  # 사용자 출처: 이용 조건에 동의한 사람(person:<id>)
    agreed_at: str | None = None


class SourceSheet(_Strict):
    kind: Literal["source"]
    id: SourceId
    title: str
    status: Literal["verified", "private"]
    owner: str
    connector: Connector
    fetch: Fetch
    item: ItemKeys
    keys: dict[str, KeyInfo] = Field(min_length=1)
    meta: dict[str, MetaInfo] = {}
    ignore: Ignore = Ignore()
    group: Literal["repost_title"] | None = None  # 같은 사업 묶기: 기관과 정정 표시를 뺀 제목
    attachments: Attachments | None = None
    terms: Terms

    def readable_keys(self) -> set[str]:
        """앱이 읽을 수 있는 키. 무시하는 키는 읽어도 변경이 오지 않으므로 뺀다."""
        return set(self.keys) - set(self.ignore.keys)

    def changed_keys(self, old: Mapping[str, Any], new: Mapping[str, Any]) -> set[str]:
        """두 원본 항목에서 값이 바뀐 키 (app-spec 9장: 변경은 앱이 읽는 것 기준).

        항목 버전을 가르는 기준(sources.base의 body_hash)과 같게 본다: 무시하는 키와 주소 키는 빼고,
        표기만 다른 글과 빈 값·없는 키는 같은 것으로. 첨부는 주소가 아니라 받은 파일로 따로 비교한다.
        """
        skip = set(self.ignore.keys) | set(self.ignore.url_keys)
        return {
            k
            for k in set(old) | set(new)
            if k not in skip and _canon(old.get(k)) != _canon(new.get(k))
        }


def _canon(v: Any) -> Any:
    v = canonical_text(v) if isinstance(v, str) else v
    return None if v in (None, "") else v


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    fix: str = ""

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" + (f" ({self.fix})" if self.fix else "")


class SheetError(ValueError):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("출처 한 장에 문제가 있다:\n" + "\n".join(f"- {p}" for p in problems))


def check_sheet(data: dict) -> tuple[SourceSheet | None, list[Problem]]:
    try:
        sheet = SourceSheet.model_validate(data)
    except ValidationError as e:
        return None, [
            Problem(".".join(str(x) for x in err["loc"]) or "(맨 위)", err["msg"])
            for err in e.errors()
        ]
    p: list[Problem] = []
    keys = set(sheet.keys)
    known = ", ".join(sorted(keys))

    def need(path: str, key: str) -> None:
        if key not in keys:
            p.append(
                Problem(
                    path,
                    f"keys에 없는 키 {key}",
                    f"keys에 설명과 함께 적거나 이름을 고친다. 있는 키: {known}",
                )
            )

    need("item.id", sheet.item.id)
    need("item.title", sheet.item.title)
    if sheet.item.url:
        need("item.url", sheet.item.url)
    for name, m in sheet.meta.items():
        need(f"meta.{name}.key", m.key)
    for k in sheet.ignore.url_keys:
        need("ignore.url_keys", k)
    if sheet.attachments:
        for k in sheet.attachments.names + sheet.attachments.urls:
            need("attachments", k)
    if sheet.item.id in sheet.ignore.keys:
        p.append(
            Problem(
                "ignore.keys",
                f"항목 ID {sheet.item.id}를 무시하면 항목을 가리킬 수 없다",
                "ignore.keys에서 뺀다",
            )
        )
    return (None if p else sheet), p


def load_sheet(path: Path | str) -> SourceSheet:
    sheet, problems = check_sheet(json.loads(Path(path).read_text(encoding="utf-8")))
    if sheet is None:
        raise SheetError(problems)
    return sheet


def load_catalog(folder: Path | str) -> dict[str, SourceSheet]:
    out: dict[str, SourceSheet] = {}
    for path in sorted(Path(folder).glob("*.json")):
        sheet = load_sheet(path)
        if sheet.id in out:
            raise SheetError([Problem("id", f"출처 {sheet.id}가 두 장 있다", path.name)])
        out[sheet.id] = sheet
    return out
