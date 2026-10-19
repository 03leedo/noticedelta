"""격리 파서 호출과 파서 출력 → 조각 블록 변환 (설계 7.3, docs/research/parsers.md).

파서는 네트워크 없는 컨테이너(infra/parser) 안에서 돈다. 이쪽에서는 파일 바이트를 표준 입력으로
넘기고, 계약(contract 1) JSON만 돌려받는다. 컨테이너 안에서 kordoc이 실패한 HWP·HWPX는
hwp-hwpx-parser로 한 번 더 시도한다.

파싱이 끝나도 쓸 만한 글이 없으면(텍스트 층 없는 스캔 PDF 등) needs_review로 보낸다.
조용히 빈 조각으로 넘기지 않는다.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from noticedelta.segments import Table, TextBlock

CONTRACT = 1
PARSEABLE_MIMES = {"application/x-hwp", "application/hwp+zip", "application/pdf"}
MIN_CHARS = 50
MIN_HANGUL_RATIO = 0.3
_MARKER = "@@PARSED@@"


@dataclass
class ParsedDocument:
    status: str  # ok / failed
    parser: str
    parser_version: str
    blocks: list[dict] = field(default_factory=list)
    warnings: int = 0
    error: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ParsedDocument:
        if data.get("contract") != CONTRACT:
            raise ValueError(f"파서 출력 계약 버전이 다름: {data.get('contract')}")
        return cls(
            status=data["status"],
            parser=data["parser"],
            parser_version=data["parser_version"],
            blocks=list(data.get("blocks") or []),
            warnings=int(data.get("warnings") or 0),
            error=data.get("error"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "status": self.status,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "blocks": self.blocks,
            "warnings": self.warnings,
            "error": self.error,
        }

    def plain_text(self) -> str:
        parts = []
        for b in self.blocks:
            if b["type"] == "table":
                parts += [c for row in b["rows"] for c in row if c]
            else:
                parts.append(b.get("text") or "")
        return "\n".join(parts)


def review_reason(doc: ParsedDocument) -> str | None:
    """운영자 검토가 필요한 이유. 없으면 None."""
    if doc.status != "ok":
        return f"파싱 실패: {doc.error}"
    text = "".join(doc.plain_text().split())
    if len(text) < MIN_CHARS:
        return f"추출한 글자가 {len(text)}자뿐 (텍스트 층 없는 스캔본일 수 있음)"
    letters = [c for c in text if c.isalpha()]
    hangul = sum("가" <= c <= "힣" for c in letters) / len(letters) if letters else 0.0
    if hangul < MIN_HANGUL_RATIO:
        return f"한글 비율 {hangul:.0%} (글꼴 대응표가 깨진 PDF일 수 있음)"
    return None


def to_blocks(doc: ParsedDocument) -> list[TextBlock | Table]:
    """계약 블록 → segments.segment_document 입력."""
    out: list[TextBlock | Table] = []
    for b in doc.blocks:
        if b["type"] == "table":
            out.append(Table(rows=[list(r) for r in b["rows"]], page=b.get("page")))
        elif b.get("text"):
            out.append(TextBlock(b["text"], heading=b["type"] == "heading", page=b.get("page")))
    return out


class Parser(Protocol):
    def parse(self, data: bytes, mime: str) -> ParsedDocument: ...


Runner = Callable[..., subprocess.CompletedProcess]


class DockerParser:
    """infra/parser 이미지를 파일마다 새 컨테이너로 돌린다.

    네트워크 없음, 읽기 전용 루트, /tmp만 쓰기, 메모리·CPU·프로세스 수 제한, 권한 제거, 비루트(이미지의 USER).
    """

    def __init__(
        self,
        image: str = "noticedelta-parser",
        *,
        timeout_s: float = 180,
        memory: str = "1g",
        cpus: str = "1",
        docker: str = "docker",
        run: Runner = subprocess.run,
    ):
        self.image = image
        self.timeout_s = timeout_s
        self.memory = memory
        self.cpus = cpus
        self.docker = docker
        self._run = run

    def command(self, mime: str) -> list[str]:
        return [
            self.docker,
            "run",
            "-i",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            self.image,
            mime,
        ]

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        try:
            p = self._run(
                self.command(mime), input=data, capture_output=True, timeout=self.timeout_s
            )
        except subprocess.TimeoutExpired:
            return ParsedDocument("failed", "container", "", error=f"{self.timeout_s:.0f}초 초과")
        out = p.stdout.decode("utf-8", "replace")
        i = out.rfind(_MARKER)
        if i < 0:
            tail = p.stderr.decode("utf-8", "replace")[-300:]
            return ParsedDocument("failed", "container", "", error=f"exit {p.returncode}: {tail}")
        return ParsedDocument.from_json(json.loads(out[i + len(_MARKER) :].splitlines()[0]))


class StaticParser:
    """테스트용. MIME별로 정해 둔 결과를 돌려준다."""

    def __init__(self, results: dict[str, ParsedDocument | Sequence[dict]]):
        self.results = results
        self.calls: list[str] = []

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        self.calls.append(mime)
        r = self.results[mime]
        if isinstance(r, ParsedDocument):
            return r
        return ParsedDocument("ok", "static", "0", blocks=list(r))
