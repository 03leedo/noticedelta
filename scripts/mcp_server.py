"""MCP 서버를 띄운다: 외부 에이전트가 플랫폼 기능을 도구로 쓴다.

    uv sync --extra mcp
    uv run python scripts/mcp_server.py                   # stdio (에이전트가 자식 프로세스로 띄운다)
    uv run python scripts/mcp_server.py --files <폴더>    # 이 폴더 안의 파일은 경로로 받는다
    uv run python scripts/mcp_server.py --list            # 내놓는 도구와 능력 속성만 본다

Claude Code에 붙이기:
    claude mcp add noticedelta -- uv --directory <저장소 폴더> run python scripts/mcp_server.py --files <폴더>

도구는 두 묶음이다: 공통 기능(문서 읽기, 두 판 비교, 글 검사)과 수집한 데이터 조회(항목, 변경).

읽기 도구(능력 속성 A)만 내놓는다. 로컬에서 내 에이전트가 쓰는 용도다. 공고 글을 발췌해 돌려주므로
공개 서버로 열지 않는다(출처 이용 조건, docs/research/terms.md).
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.doc_tools import doc_tools  # noqa: E402
from noticedelta.item_tools import item_tools  # noqa: E402
from noticedelta.parsing import DockerParser  # noqa: E402
from noticedelta.programs import api_meta  # noqa: E402
from noticedelta.tooling import CAPABILITIES, Registry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="도구 목록만 찍고 끝낸다")
    ap.add_argument(
        "--files",
        action="append",
        default=[],
        help="경로로 받을 파일이 있는 폴더(여러 번 줄 수 있다)",
    )
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    registry = Registry(allowed=("A",))
    roots = [Path(p) for p in args.files]
    for tool in [*doc_tools(DockerParser(), roots), *item_tools(factory, meta=api_meta)]:
        registry.add(tool)
    if args.list:
        for t in registry.tools.values():
            caps = ", ".join(f"{c}({CAPABILITIES[c]})" for c in sorted(t.capabilities))
            print(f"{t.name}: {t.description} [{caps}]")
        return
    from noticedelta.mcp_adapter import build_server

    build_server(registry).run("stdio")


if __name__ == "__main__":
    main()
