"""운영자가 관리자를 둔다 (managers.py): 기관을 확인한 뒤, 그 사람의 링크와 보일 이름으로.

    uv run python scripts/grant_manager.py --link https://…/p/<토큰> --label "부산창업지원단"

구독자에게는 이 이름이 보인다. 확인하지 않은 사람은 초대를 만들 수 없다(사칭 방지).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
from noticedelta import managers, people  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.grants import APP  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", required=True, help="관리자가 될 사람의 링크(주소 또는 토큰)")
    ap.add_argument("--label", required=True, help="구독자에게 보일 이름(기관 이름)")
    ap.add_argument("--app", default=APP.id)
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    path = urlsplit(args.link).path.rstrip("/")
    token = path.rsplit("/p/", 1)[-1].split("/", 1)[0] if "/p/" in path else args.link.strip()
    with factory() as s:
        person_id = people.resolve(s, token)
        if person_id is None:
            raise SystemExit("링크가 없거나 끊겼습니다")
        managers.grant(s, args.app, person_id, label=args.label, at=datetime.now(UTC))
        s.commit()
    print(f"{args.app}의 관리자로 두었습니다: {args.label} ({person_id[:8]})")


if __name__ == "__main__":
    main()
