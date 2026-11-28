"""파일 올리기 출처 (source-catalog.md 3장의 `file_drop`): 사람이 목록 파일의 새 판을 올린다.

    rows = parse(data, "csv")                  # CSV(UTF-8·CP949) 또는 JSON(객체의 배열)
    guess = propose(rows)                      # 항목 id·제목·주소로 쓸 칸을 제안한다
    convert = to_record(source_id, sheet)      # 줄(칸 → 값) → NoticeRecord

- 판(올린 파일 하나)은 그때의 목록 전체다. 판의 순서는 올린 차례다(user_sources.drop). 같은 파일을
  다시 올리면 새 판이 아니다.
- 칸 이름이 곧 키다. 장(sheet)의 keys에 적은 칸만 받는다(새 칸은 장을 고칠 때까지 무시한다).
- 항목 id 칸은 판 안에서 비지 않고 겹치지 않아야 한다. 판마다 같은 id가 같은 항목이다.
- 받은 값은 신뢰할 수 없는 입력(A)이다. 크기·줄·칸 수에 한도를 둔다. 첨부는 받지 않는다(1차).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from noticedelta.sources.base import NoticeRecord, SourceError

MAX_BYTES = 2_000_000
MAX_ROWS = 2000
MAX_COLUMNS = 50
MAX_CELL = 5000
MAX_NAME = 64
FORMATS = ("csv", "json")
_ID_NAME = re.compile(r"(^|_)(id|no|key|code)$|번호|코드|아이디", re.I)
_TITLE_NAME = re.compile(r"title|name|제목|사업명|공고명|이름|명칭", re.I)


def format_of(filename: str, data: bytes) -> str:
    """파일 이름의 확장자로, 없으면 내용의 첫 글자로 가른다."""
    name = filename.lower()
    if name.endswith(".json"):
        return "json"
    if name.endswith((".csv", ".txt")):
        return "csv"
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    return "json" if head in (b"[", b"{") else "csv"


def decode(data: bytes) -> str:
    """UTF-8(BOM 포함)로, 아니면 한국 공공 기관의 엑셀이 흔히 내는 CP949로 읽는다."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def _name(key: Any) -> str:
    return " ".join(str(key).split())[:MAX_NAME]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value).strip()[:MAX_CELL]
    raise SourceError("JSON의 값은 글·숫자·참거짓·빈 값만 받는다(안에 객체나 배열을 넣지 않는다)")


def parse(data: bytes, fmt: str) -> list[dict[str, str]]:
    """판 하나의 줄들. 칸 이름은 공백을 하나로 줄이고, 값은 글로 맞춘다. 고칠 것이 있으면 SourceError."""
    if fmt not in FORMATS:
        raise SourceError(f"모르는 형식: {fmt} (csv, json)")
    if len(data) > MAX_BYTES:
        raise SourceError(f"파일이 너무 크다({MAX_BYTES // 1_000_000}MB까지)")
    text = decode(data)
    if fmt == "json":
        try:
            body = json.loads(text)
        except json.JSONDecodeError as e:
            raise SourceError(f"JSON을 읽지 못했다: {e.msg} (줄 {e.lineno})") from None
        except (ValueError, RecursionError) as e:  # 아주 큰 정수, 너무 깊은 중첩
            raise SourceError(f"JSON을 읽지 못했다: {type(e).__name__}") from None
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            body = body["items"]
        if not isinstance(body, list) or not all(isinstance(r, dict) for r in body):
            raise SourceError('JSON은 객체의 배열이어야 한다(예: [{"id": 1, "title": …}])')
        raw_rows = body
    else:
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            raw_rows = list(reader)
        except csv.Error as e:
            raise SourceError(f"CSV를 읽지 못했다: {e}") from None
        header = [_name(h) for h in (reader.fieldnames or []) if h is not None]
        dup = [h for h, n in Counter(h for h in header if h).items() if n > 1]
        if dup:
            raise SourceError(f"같은 이름의 칸이 둘 있다: {dup[:3]}")
    if len(raw_rows) > MAX_ROWS:
        raise SourceError(f"줄이 너무 많다({MAX_ROWS}줄까지)")
    rows = []
    for r in raw_rows:
        names = [_name(k) for k in r if k is not None and _name(k)]
        dup = [h for h, n in Counter(names).items() if n > 1]
        if dup:  # 공백만 다른 칸 이름: 한쪽 값이 말없이 덮이지 않게
            raise SourceError(f"같은 이름의 칸이 둘 있다: {dup[:3]}")
        row = {_name(k): _cell(v) for k, v in r.items() if k is not None and _name(k)}
        if len(row) > MAX_COLUMNS:
            raise SourceError(f"칸이 너무 많다({MAX_COLUMNS}칸까지)")
        if any(row.values()):  # 빈 줄은 건너뛴다
            rows.append(row)
    if not rows:
        raise SourceError("줄이 하나도 없다")
    return rows


def columns(rows: list[dict[str, str]]) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        seen.update(dict.fromkeys(r))
    return list(seen)


def unique(rows: list[dict[str, str]], col: str) -> bool:
    values = [r.get(col, "") for r in rows]
    return all(values) and len(set(values)) == len(values)


def id_problem(rows: list[dict[str, str]], col: str) -> str | None:
    """항목 id 칸으로 쓸 수 없는 이유(값은 드러내지 않는다)."""
    values = [r.get(col, "") for r in rows]
    empty = sum(1 for v in values if not v)
    if empty:
        return f"항목 id 칸({col})이 빈 줄이 {empty}개 있다"
    dup = sum(n - 1 for n in Counter(values).values() if n > 1)
    if dup:
        return f"항목 id 칸({col})에 겹치는 값이 {dup}개 있다(판 안에서 줄마다 달라야 한다)"
    return None


def propose(rows: list[dict[str, str]]) -> dict[str, str | None]:
    """항목 id·제목·주소로 쓸 칸. id는 비지 않고 겹치지 않는 칸(이름이 id·번호 같은 것을 먼저)."""
    cols = columns(rows)
    ids = [c for c in cols if unique(rows, c)]
    ids.sort(key=lambda c: (not _ID_NAME.search(c), cols.index(c)))
    id_col = ids[0] if ids else None

    def avg_len(c: str) -> float:
        return sum(len(r.get(c, "")) for r in rows) / len(rows)

    def numeric(c: str) -> bool:
        return all(re.fullmatch(r"[\d.,\-\s]*", r.get(c, "")) for r in rows)

    texts = [c for c in cols if c != id_col and not numeric(c)]
    texts.sort(key=lambda c: (not _TITLE_NAME.search(c), -avg_len(c)))

    def link(c: str) -> bool:  # 값이 있는 줄이 모두 http(s) 주소인 칸(비어 있는 줄은 괜찮다)
        filled = [r[c] for r in rows if r.get(c)]
        return bool(filled) and all(v.startswith(("http://", "https://")) for v in filled)

    links = [c for c in cols if c != id_col and link(c)]
    return {
        "id": id_col,
        "title": texts[0] if texts else id_col,
        "url": links[0] if links else None,
    }


def to_record(source_id: str, sheet) -> Callable[[dict], NoticeRecord]:
    """줄을 NoticeRecord로. 장의 keys에 적은 칸만 원본(raw)에 넣는다."""
    keys = list(sheet.keys)
    item = sheet.item
    ignore = frozenset(sheet.ignore.keys)
    url_keys = frozenset(sheet.ignore.url_keys)

    def convert(row: dict) -> NoticeRecord:
        rid = str(row.get(item.id) or "").strip()
        if not rid:
            raise SourceError(f"항목 id 칸({item.id})이 비었다")
        if len(rid) > 100:  # 게시물 id 칸에 맞춘다(자르면 겹칠 수 있어 해시로)
            rid = "h:" + hashlib.sha256(rid.encode("utf-8")).hexdigest()[:40]
        raw = {k: row.get(k, "") for k in keys}
        link = str(row.get(item.url) or "") if item.url else ""
        body = "\n".join(f"{k}: {v}" for k, v in raw.items() if v and k != item.id)
        return NoticeRecord(
            source=source_id,
            external_id=rid,
            title=str(row.get(item.title) or "").strip()[:500] or rid[:100],
            org=None,  # 기관이 없으면 항목을 묶지 않는다(줄 하나가 항목 하나)
            url=link if link.startswith(("http://", "https://")) else None,
            body=body,
            apply_start=None,
            apply_end=None,
            raw=raw,
            ignore=ignore,  # 장의 ignore.keys: 바뀌어도 새 판이 아니다
            url_keys=url_keys,
        )

    return convert
