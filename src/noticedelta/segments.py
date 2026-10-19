"""공고 본문·첨부를 문단·표 칸 단위 조각으로 나눈다 (설계 7.3)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

SegmentKind = Literal["heading", "paragraph", "table_cell"]

HEADING_MARKERS = ("□", "■", "◆", "◇", "Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ")
LIST_MARKERS = ("○", "◦", "•", "·", "-", "ㅇ", "※", "▶", "*")
HEADING_MAX_LEN = 30
_NUMBERED_HEADING = re.compile(r"^(\d{1,2}|[가-하])[.)]\s")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Segment:
    kind: SegmentKind
    text: str
    text_hash: str
    locator: dict
    order: int
    lineage_id: str | None = None


@dataclass(frozen=True)
class Table:
    """첫 행이 머리글인 표.

    병합 칸이 덮는 자리는 None이다. None 자리는 조각을 만들지 않고, 행 키·열 머리글을 정할 때만
    위(행 병합)나 왼쪽(열 병합) 칸의 값으로 채워 본다.
    """

    rows: list[list[str | None]]
    caption: str = ""
    page: int | None = None


@dataclass(frozen=True)
class TextBlock:
    """파서가 준 문단·제목 하나. 파서가 제목이라고 알려 주면 그대로 제목 조각이 된다."""

    text: str
    heading: bool = False
    page: int | None = None


@dataclass
class _Draft:
    kind: SegmentKind
    text: str
    locator: dict = field(default_factory=dict)


def normalize_text(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", text)).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _is_heading(line: str) -> bool:
    if len(line) > HEADING_MAX_LEN:
        return False
    return line.startswith(HEADING_MARKERS) or bool(_NUMBERED_HEADING.match(line))


def _split_chunk(chunk: str) -> list[str]:
    """빈 줄 없는 덩어리를 목록 기호 기준으로 나눈다. 기호 없는 줄은 앞 항목에 붙인다."""
    items: list[str] = []
    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not items or line.startswith(LIST_MARKERS) or _is_heading(line):
            items.append(line)
        else:
            items[-1] = f"{items[-1]} {line}"
    return items


def _text_drafts(
    block: str, *, source: str, block_no: int, heading: bool = False, page: int | None = None
) -> list[_Draft]:
    drafts: list[_Draft] = []
    loc: dict = {"source": source, "block": block_no}
    if page is not None:
        loc["page"] = page
    for chunk in re.split(r"\n\s*\n", block):
        for item in _split_chunk(chunk):
            text = normalize_text(item)
            kind: SegmentKind = "heading" if heading or _is_heading(text) else "paragraph"
            drafts.append(_Draft(kind, text, dict(loc)))
    return drafts


def _fill_merged(rows: list[list[str | None]]) -> list[list[str]]:
    """None(병합으로 덮인 자리)을 위쪽 칸, 첫 행이면 왼쪽 칸 값으로 채운 사본."""
    out: list[list[str]] = []
    for row in rows:
        filled: list[str] = []
        for c, cell in enumerate(row):
            if cell is not None:
                filled.append(cell)
            elif out and c < len(out[-1]):
                filled.append(out[-1][c])
            elif filled:
                filled.append(filled[-1])
            else:
                filled.append("")
        out.append(filled)
    return out


def _table_drafts(table: Table, *, source: str, table_no: int) -> list[_Draft]:
    if not table.rows:
        return []
    filled = _fill_merged(table.rows)
    header = [normalize_text(h) for h in filled[0]]
    sig = "|".join(header)
    # 첫 열이 세로 병합(분류 칸)이면 첫 열만으로는 행이 겹친다. 둘째 열까지 행 키로 쓴다.
    merged_first = any(row and row[0] is None for row in table.rows[1:])
    drafts: list[_Draft] = []
    for r, row in enumerate(table.rows[1:], start=1):
        keys = filled[r]
        row_key = normalize_text(keys[0]) if keys else ""
        if merged_first and len(keys) > 1:
            row_key = f"{row_key} / {normalize_text(keys[1])}"
        for c, cell in enumerate(row):
            if cell is None:
                continue
            loc = {
                "source": source,
                "table": table_no,
                "row": r,
                "col": c,
                "row_key": row_key,
                "header": header[c] if c < len(header) else "",
                "table_sig": sig,
            }
            if table.page is not None:
                loc["page"] = table.page
            drafts.append(_Draft("table_cell", normalize_text(cell), loc))
    return drafts


def segment_document(
    blocks: Sequence[str | TextBlock | Table], *, source: str, start_order: int = 0
) -> list[Segment]:
    """블록들을 조각으로. 빈 표 칸도 조각으로 남긴다(빈 칸에 값이 생기는 것도 변경이다).

    여러 문서(본문, 첨부들)를 이어 붙일 때는 start_order로 순서를 이어 준다.
    """
    drafts: list[_Draft] = []
    table_no = 0
    for block_no, block in enumerate(blocks):
        if isinstance(block, Table):
            drafts.extend(_table_drafts(block, source=source, table_no=table_no))
            table_no += 1
        elif isinstance(block, TextBlock):
            drafts.extend(
                _text_drafts(
                    block.text,
                    source=source,
                    block_no=block_no,
                    heading=block.heading,
                    page=block.page,
                )
            )
        else:
            drafts.extend(_text_drafts(block, source=source, block_no=block_no))
    return [
        Segment(d.kind, d.text, text_hash(d.text), d.locator, order)
        for order, d in enumerate(drafts, start=start_order)
    ]
