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
    """첫 행이 머리글인 표."""

    rows: list[list[str]]
    caption: str = ""


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


def _text_drafts(block: str, *, source: str, block_no: int) -> list[_Draft]:
    drafts: list[_Draft] = []
    for chunk in re.split(r"\n\s*\n", block):
        for item in _split_chunk(chunk):
            text = normalize_text(item)
            kind: SegmentKind = "heading" if _is_heading(text) else "paragraph"
            drafts.append(_Draft(kind, text, {"source": source, "block": block_no}))
    return drafts


def _table_drafts(table: Table, *, source: str, table_no: int) -> list[_Draft]:
    if not table.rows:
        return []
    header = [normalize_text(h) for h in table.rows[0]]
    sig = "|".join(header)
    drafts: list[_Draft] = []
    for r, row in enumerate(table.rows[1:], start=1):
        row_key = normalize_text(row[0]) if row else ""
        for c, cell in enumerate(row):
            drafts.append(
                _Draft(
                    "table_cell",
                    normalize_text(cell),
                    {
                        "source": source,
                        "table": table_no,
                        "row": r,
                        "col": c,
                        "row_key": row_key,
                        "header": header[c] if c < len(header) else "",
                        "table_sig": sig,
                    },
                )
            )
    return drafts


def segment_document(blocks: Sequence[str | Table], *, source: str) -> list[Segment]:
    drafts: list[_Draft] = []
    table_no = 0
    for block_no, block in enumerate(blocks):
        if isinstance(block, Table):
            drafts.extend(_table_drafts(block, source=source, table_no=table_no))
            table_no += 1
        else:
            drafts.extend(_text_drafts(block, source=source, block_no=block_no))
    return [
        Segment(d.kind, d.text, text_hash(d.text), d.locator, order)
        for order, d in enumerate(drafts)
    ]
