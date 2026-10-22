"""격리 파서 호출과 파서 출력 → 조각 블록 변환 (설계 7.3, docs/research/parsers.md).

파서는 네트워크 없는 컨테이너(infra/parser) 안에서 돈다. 이쪽에서는 파일 바이트를 표준 입력으로
넘기고, 계약(contract 1) JSON만 돌려받는다. 컨테이너 안에서 kordoc이 실패한 HWP·HWPX는
hwp-hwpx-parser로 한 번 더 시도한다.

파싱이 끝나도 쓸 만한 글이 없으면(텍스트 층 없는 스캔 PDF 등) needs_review로 보낸다.
조용히 빈 조각으로 넘기지 않는다.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from noticedelta.segments import Table, TextBlock

CONTRACT = 1
PARSEABLE_MIMES = {"application/x-hwp", "application/hwp+zip", "application/pdf"}
MIN_CHARS = 50
MIN_HANGUL_RATIO = 0.3
_MARKER = "@@PARSED@@"


_BLOCK_TYPES = {"heading", "paragraph", "table"}
MAX_BLOCKS = 100_000


def _check_blocks(blocks: Any) -> None:
    """파서는 신뢰할 수 없는 입력을 다루는 쪽이다. 출력 모양을 믿지 않고 확인한다."""
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise ValueError("blocks 형식 오류")
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") not in _BLOCK_TYPES:
            raise ValueError("알 수 없는 블록")
        if b["type"] == "table":
            rows = b.get("rows")
            if not isinstance(rows, list) or not all(
                isinstance(r, list) and all(c is None or isinstance(c, str) for c in r)
                for r in rows
            ):
                raise ValueError("표 형식 오류")
        elif not isinstance(b.get("text", ""), str):
            raise ValueError("문단 형식 오류")


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
        _check_blocks(data.get("blocks") or [])
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
            rows = [[None if c is None else _clean(c) for c in r] for r in b["rows"]]
            out.append(Table(rows=rows, page=b.get("page")))
        elif b.get("text"):
            text = _clean(b["text"])
            out.append(TextBlock(text, heading=b["type"] == "heading", page=b.get("page")))
    return out


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _clean(text: str) -> str:
    """깨진 HWP(UTF-16)에서 나오는 짝 없는 서로게이트는 UTF-8로 인코딩되지 않는다. U+FFFD로 바꾼다."""
    return _LONE_SURROGATE.sub("�", text)


class Parser(Protocol):
    def parse(self, data: bytes, mime: str) -> ParsedDocument: ...


Runner = Callable[..., subprocess.CompletedProcess]

MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_INFRA_EXIT_CODES = {125, 126, 127}  # docker run 자체 실패, 실행 불가, 명령 없음
_DOCKER_DOWN = (
    "connect to the docker"  # "Cannot connect to the Docker daemon", "failed to connect …"
)


class ParserUnavailable(RuntimeError):
    """파서를 돌릴 수 없는 인프라 문제(Docker 꺼짐, 이미지 없음). 첨부는 pending으로 두고 나중에 다시 돈다."""


def _result_line(out: str) -> str | None:
    """표지로 시작하는 마지막 줄의 JSON.

    파서는 결과를 "\\n@@PARSED@@<JSON>\\n"으로 쓴다. JSON 안의 개행은 이스케이프되므로 줄 머리에
    표지가 올 수 있는 곳은 진짜 결과뿐이다. 본문에 표지 문자열이 들어 있어도(줄 중간), U+2028 같은
    문자가 있어도(\\n으로만 자른다) 영향이 없다.
    """
    for line in reversed(out.split("\n")):
        line = line.rstrip("\r")
        if line.startswith(_MARKER):
            return line[len(_MARKER) :]
    return None


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

    def command(self, mime: str, name: str = "nd-parse") -> list[str]:
        return [
            self.docker,
            "run",
            "-i",
            "--rm",
            "--name",
            name,  # 시간 초과 때 docker kill로 찾아 끝내려고 붙인다
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:size=256m",
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,  # 메모리와 같게 두어 swap을 쓰지 않는다
            "--log-driver",
            "none",
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

    def _kill(self, name: str) -> None:
        # docker CLI만 죽으면 컨테이너는 계속 돈다(평가 문서 #6에서 재현). 이름으로 직접 끝낸다.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._run([self.docker, "kill", name], input=None, capture_output=True, timeout=30)

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        name = f"nd-parse-{uuid.uuid4().hex[:12]}"
        try:
            p = self._run(
                self.command(mime, name), input=data, capture_output=True, timeout=self.timeout_s
            )
        except FileNotFoundError:
            raise ParserUnavailable(f"{self.docker} 명령을 찾을 수 없음") from None
        except subprocess.TimeoutExpired:
            self._kill(name)
            return ParsedDocument("failed", "container", "", error=f"{self.timeout_s:.0f}초 초과")
        if len(p.stdout) > MAX_OUTPUT_BYTES:
            return ParsedDocument("failed", "container", "", error="파서 출력이 상한을 넘음")
        line = _result_line(p.stdout.decode("utf-8", "replace"))
        if line is None:
            tail = p.stderr.decode("utf-8", "replace")[-300:]
            if p.returncode in _INFRA_EXIT_CODES or _DOCKER_DOWN in tail.lower():
                # Docker가 꺼졌거나 이미지가 없다. 문서 탓이 아니므로 needs_review로 굳히지 않는다.
                raise ParserUnavailable(f"exit {p.returncode}: {tail}")
            return ParsedDocument("failed", "container", "", error=f"exit {p.returncode}: {tail}")
        try:
            return ParsedDocument.from_json(json.loads(line))
        except (ValueError, KeyError, TypeError) as e:
            return ParsedDocument("failed", "container", "", error=f"파서 출력 오류: {e}"[:300])


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
