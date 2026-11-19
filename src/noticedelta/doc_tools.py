"""공통 기능 도구 (ARCHITECTURE 3.2, 10.1). 에이전트가 문서를 맡겨 플랫폼 기능을 빌려 쓴다.

    read_document      파일(HWP·HWPX·PDF)이나 글을 격리 파서로 읽어 조각(문단·표 칸)으로 준다
    compare_documents  두 판을 조각으로 맞춰 바뀐 곳과 바뀐 값(날짜·금액)을 준다. LLM을 쓰지 않는다
    check_text         개인정보를 가린 글과, 숨은 지시로 보이는 줄의 위치를 준다

문서는 세 가지로 받는다: 글(text), 허락한 폴더 안의 파일 경로(path), 파일 내용(file_base64).
경로는 서버를 띄울 때 허락한 폴더(roots) 안만 읽는다. 허락한 폴더가 없으면 경로로는 받지 않는다.
모두 능력 속성 A다. 맡긴 문서가 신뢰할 수 없는 글일 수 있기 때문이다.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from noticedelta.history import counter_ids
from noticedelta.llm.harness import mask, scan
from noticedelta.matching import match_segments
from noticedelta.parsing import (
    PARSEABLE_MIMES,
    Parser,
    ParserUnavailable,
    review_reason,
    to_blocks,
)
from noticedelta.segments import Segment, segment_document
from noticedelta.sources.attachments import sniff_mime
from noticedelta.tooling import Tool
from noticedelta.values import find_amounts, find_datetimes

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
MAX_ITEMS = 300  # read_document가 한 번에 돌려줄 조각 수
MAX_PAIRS = 100
CLIP = 2000  # 조각 하나에서 돌려줄 글자 수

READS_UNTRUSTED = frozenset({"A"})


class DocumentError(ValueError):
    """맡긴 문서를 읽지 못했다. 메시지를 그대로 에이전트에게 돌려준다."""


def _clip(text: str, n: int = CLIP) -> str:
    return text if len(text) <= n else text[:n] + "…"


def where(locator: dict) -> str:
    """조각 위치를 사람이 읽는 말로: "3쪽 표2 2행 신청자격"."""
    parts = []
    if locator.get("page"):
        parts.append(f"{locator['page']}쪽")
    if "table" in locator:
        parts.append(f"표{locator['table'] + 1}")
        if locator.get("row_key"):
            parts.append(str(locator["row_key"]))
        if locator.get("header"):
            parts.append(str(locator["header"]))
    elif "block" in locator:
        parts.append(f"블록 {locator['block'] + 1}")
    return " ".join(parts) or "본문"


def values_in(text: str) -> dict[str, list]:
    """글 안의 날짜와 금액. 값이 바뀐 조각과 문구만 바뀐 조각을 가른다."""
    dates = sorted({d.strftime("%Y-%m-%d %H:%M") for d in find_datetimes(text)})
    return {"dates": dates, "amounts": sorted(set(find_amounts(text)))}


class DocumentReader:
    """세 가지 입력을 받아 조각으로 만든다."""

    def __init__(self, parser: Parser | None, roots: Sequence[Path] = ()):
        self.parser = parser
        self.roots = [Path(r).resolve() for r in roots]

    def _bytes(self, path: str, file_base64: str) -> tuple[bytes, str]:
        if path:
            if not self.roots:
                raise DocumentError(
                    "경로로 받는 문서를 허락하지 않았다. file_base64나 text로 보낸다"
                )
            p = Path(path).resolve()
            if not any(p.is_relative_to(root) for root in self.roots):
                allowed = ", ".join(str(r) for r in self.roots)
                raise DocumentError(f"허락한 폴더 밖의 파일이다. 읽을 수 있는 곳: {allowed}")
            if not p.is_file():
                raise DocumentError(f"파일이 없다: {p.name}")
            if p.stat().st_size > MAX_FILE_BYTES:
                raise DocumentError(f"파일이 {MAX_FILE_BYTES // 1024 // 1024}MB보다 크다")
            return p.read_bytes(), p.name
        if len(file_base64) > MAX_FILE_BYTES * 4 // 3 + 16:
            raise DocumentError(f"파일이 {MAX_FILE_BYTES // 1024 // 1024}MB보다 크다")
        try:
            return base64.b64decode(file_base64, validate=True), ""
        except (binascii.Error, ValueError):
            raise DocumentError("file_base64가 base64가 아니다") from None

    def load(
        self, *, text: str = "", path: str = "", file_base64: str = "", filename: str = ""
    ) -> tuple[list[Segment], dict]:
        """(조각, 읽은 방법). 셋 중 하나만 준다."""
        given = [k for k, v in (("text", text), ("path", path), ("file_base64", file_base64)) if v]
        if len(given) != 1:
            raise DocumentError("text, path, file_base64 가운데 하나만 준다")
        if text:
            if len(text) > MAX_TEXT_CHARS:
                raise DocumentError(f"글이 {MAX_TEXT_CHARS:,}자보다 길다")
            return segment_document([text], source="text"), {"kind": "text"}
        data, name = self._bytes(path, file_base64)
        mime = sniff_mime(data, filename or name)
        if mime not in PARSEABLE_MIMES:
            raise DocumentError(f"읽을 수 없는 형식이다({mime}). HWP, HWPX, PDF를 읽는다")
        if self.parser is None:
            raise DocumentError("이 서버에는 파서가 없다")
        try:
            doc = self.parser.parse(data, mime)  # 네트워크 없는 격리 컨테이너
        except ParserUnavailable:
            raise DocumentError(
                "파서(Docker)를 쓸 수 없다. Docker가 켜져 있는지 확인한다"
            ) from None
        reason = review_reason(doc)
        if doc.status != "ok":
            raise DocumentError(reason or "파싱에 실패했다")
        segments = segment_document(to_blocks(doc), source="file")
        info = {"kind": mime, "parser": f"{doc.parser} {doc.parser_version}".strip()}
        if reason:
            info["warning"] = reason  # 글자가 거의 없는 스캔본 등. 조각은 그대로 준다
        return segments, info


def doc_tools(parser: Parser | None, roots: Sequence[Path] = ()) -> list[Tool]:
    reader = DocumentReader(parser, roots)

    def read_document(
        text: str = "",
        path: str = "",
        file_base64: str = "",
        filename: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """문서를 조각(문단·제목·표 칸)으로 나눠 위치와 함께 준다. offset·limit으로 나눠 받는다."""
        try:
            segments, info = reader.load(
                text=text, path=path, file_base64=file_base64, filename=filename
            )
        except DocumentError as e:
            return {"error": str(e)}
        start = max(0, offset)
        shown = segments[start : start + max(1, min(limit, MAX_ITEMS))]
        kinds = Counter(s.kind for s in segments)
        return {
            **info,
            "segments": len(segments),
            "by_kind": dict(kinds),
            "items": [
                {"kind": s.kind, "where": where(s.locator), "text": _clip(s.text)} for s in shown
            ],
            "more": max(0, len(segments) - start - len(shown)),
        }

    def compare_documents(
        before_text: str = "",
        before_path: str = "",
        before_file_base64: str = "",
        after_text: str = "",
        after_path: str = "",
        after_file_base64: str = "",
        max_pairs: int = 30,
    ) -> dict:
        """두 판을 비교해 바뀐 조각과, 그 안에서 바뀐 날짜·금액을 준다. 값이 바뀐 곳을 먼저 준다."""
        try:
            old, old_info = reader.load(
                text=before_text, path=before_path, file_base64=before_file_base64
            )
            new, new_info = reader.load(
                text=after_text, path=after_path, file_base64=after_file_base64
            )
        except DocumentError as e:
            return {"error": str(e)}
        ids = counter_ids()
        first = match_segments([], old, new_id=ids)
        change = match_segments(first.segments, new, new_id=ids)
        before = {s.lineage_id: s for s in first.segments}
        after = {s.lineage_id: s for s in change.segments}

        pairs = []
        for kind, ids_ in (
            ("modified", change.modified),
            ("added", change.added),
            ("removed", change.removed),
        ):
            for lid in ids_:
                a, b = before.get(lid), after.get(lid)
                va = values_in(a.text) if a else {"dates": [], "amounts": []}
                vb = values_in(b.text) if b else {"dates": [], "amounts": []}
                moved = {k: {"before": va[k], "after": vb[k]} for k in va if va[k] != vb[k]}
                pairs.append(
                    {
                        "change": kind,
                        "where": where((b or a).locator),
                        "before": _clip(a.text, 500) if a else None,
                        "after": _clip(b.text, 500) if b else None,
                        "values": moved,
                        "_order": (b or a).order,
                    }
                )
        # 값(날짜·금액)이 바뀐 조각 먼저, 그다음 문서 순서
        pairs.sort(key=lambda p: (not p["values"], p["_order"]))
        for p in pairs:
            del p["_order"]
        modified = [p for p in pairs if p["change"] == "modified"]
        shown = pairs[: max(1, min(max_pairs, MAX_PAIRS))]
        return {
            "before": {**old_info, "segments": len(old)},
            "after": {**new_info, "segments": len(new)},
            "summary": {
                "added": len(change.added),
                "removed": len(change.removed),
                "modified": len(change.modified),
                "value_changed": sum(bool(p["values"]) for p in pairs),
                "wording_only": sum(not p["values"] for p in modified),
            },
            "pairs": shown,
            "more": len(pairs) - len(shown),
        }

    def check_text(text: str, mask_pii: bool = True) -> dict:
        """개인정보(주민·사업자번호, 전화, 이메일)를 가린 글과, 숨은 지시로 보이는 줄을 준다.

        가린 값의 원래 값은 돌려주지 않는다. 다른 곳에 넘기기 전에 글을 걸러 내는 용도다.
        """
        if len(text) > MAX_TEXT_CHARS:
            return {"error": f"글이 {MAX_TEXT_CHARS:,}자보다 길다"}
        masked, table = mask(text)
        pii = Counter(k[2:].split("_")[0] for k in table)
        lines = [
            {"line": i, "rules": rules}
            for i, line in enumerate(text.split("\n"), 1)
            if (rules := scan(line))
        ]
        return {
            "masked": masked if mask_pii else None,
            "pii": dict(pii),
            "suspicious_lines": lines,
        }

    described = [
        (
            read_document,
            "HWP·HWPX·PDF 파일이나 글을 네트워크 없는 격리 파서로 읽어, 문단·제목·표 칸 단위 조각과 "
            "위치(쪽, 표, 행, 열 머리글)를 준다.",
        ),
        (
            compare_documents,
            "같은 문서의 두 판을 조각 단위로 맞춰 추가·삭제·수정된 곳을 찾고, 수정된 조각에서 바뀐 "
            "날짜·금액을 따로 준다. 값이 그대로면 문구만 바뀐 것으로 본다. LLM을 쓰지 않는다.",
        ),
        (
            check_text,
            "글에서 개인정보(주민등록번호, 사업자등록번호, 전화, 이메일)를 가리고, 모델에게 내리는 "
            "지시로 보이는 줄(프롬프트 인젝션)의 줄 번호를 준다.",
        ),
    ]
    # 사용 기록이 결과 개수를 세는 칸. 맡긴 문서는 사람의 것이라 인자는 남기지 않는다
    counts = {
        "read_document": "segments",
        "compare_documents": "pairs",
        "check_text": "suspicious_lines",
    }
    return [
        Tool(fn.__name__, text, fn, READS_UNTRUSTED, count=counts[fn.__name__])
        for fn, text in described
    ]
