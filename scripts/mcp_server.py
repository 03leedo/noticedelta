"""MCP 서버를 띄운다: 외부 에이전트가 플랫폼 기능을 도구로 쓴다.

    uv sync --extra mcp
    uv run python scripts/mcp_server.py                   # stdio (에이전트가 자식 프로세스로 띄운다)
    uv run python scripts/mcp_server.py --files <폴더>    # 이 폴더 안의 파일은 경로로 받는다
    uv run python scripts/mcp_server.py --list            # 내놓는 도구와 능력 속성·효과만 본다

Claude Code에 붙이기:
    claude mcp add noticedelta -- uv --directory <저장소 폴더> run python scripts/mcp_server.py --files <폴더>

내 정보 도구(get_profile, check, explain)를 쓰려면 .env에 내 링크 토큰을 둔다(NOTICEDELTA_MCP_LINK,
웹 주소 /p/<토큰>의 토큰). 웹의 "내 정보 고치기"에서 에이전트 도우미(선택)에 동의해야 값이 나온다.
토큰은 명령줄 인자로 주지 않는다(프로세스 목록에 보인다).

모든 호출은 도구 게이트웨이(tool_gateway)를 지난다. 접속 하나가 (사람, 앱, 에이전트 이름) 세션을 잇고,
다시 접속해도 세션의 속성(외부 글을 봤다 A, 내 정보를 읽었다 B)이 남는다. 호출은 사용 기록으로도
남는다(scripts/usage_report.py). 공고 글을 발췌해 돌려주므로 공개 서버로 열지 않는다
(출처 이용 조건, docs/research/terms.md).
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta import people  # noqa: E402
from noticedelta.agent import build_gateway  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.grants import APP  # noqa: E402
from noticedelta.parsing import DockerParser  # noqa: E402
from noticedelta.tooling import CAPABILITIES  # noqa: E402
from noticedelta.usage import UsageLog  # noqa: E402
from noticedelta.vault import load_cipher  # noqa: E402

LINK_ENV = "NOTICEDELTA_MCP_LINK"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="도구 목록만 찍고 끝낸다")
    ap.add_argument(
        "--files",
        action="append",
        default=[],
        help="경로로 받을 파일이 있는 폴더(여러 번 줄 수 있다)",
    )
    ap.add_argument(
        "--client", default="mcp-stdio", help="사용 기록과 세션에 남길 접속 이름(예: claude-code)"
    )
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    now = datetime.now(UTC)
    person_id = None
    if token := os.environ.get(LINK_ENV):
        with factory() as s:
            person_id = people.resolve(s, token, at=now)
            s.commit()
        if person_id is None:
            raise SystemExit(f"{LINK_ENV}의 링크가 없거나 끊겼습니다")
    usage = UsageLog(factory, client=args.client)  # 도구 호출과 피드백을 남긴다(usage.py)
    gate = build_gateway(
        factory,
        parser=DockerParser(),
        roots=[Path(p) for p in args.files],
        cipher=load_cipher(ROOT / "data" / "vault.key") if person_id else None,
        usage=usage,
    )
    if args.list:
        for t in gate.tools.values():
            caps = ", ".join(f"{c}({CAPABILITIES[c]})" for c in sorted(t.capabilities))
            print(f"{t.name}: {t.description} [{caps or '없음'} · 효과 {t.effect}]")
        print("내 정보 도구: " + ("켜짐" if person_id else f"꺼짐({LINK_ENV} 없음)"))
        return
    from noticedelta.mcp_adapter import build_gateway_server

    with factory() as s:
        sid = gate.open_session(
            s, app_id=APP.id, agent=args.client, person_id=person_id, now=now
        ).id
        s.commit()
    build_gateway_server(gate, factory, sid).run("stdio")


if __name__ == "__main__":
    main()
