"""앱 상태 보고 (app-spec 11장): 앱마다 수집·형식 변화·파싱·붙잡힌 알림·예산을 사람 말로 요약한다.

    uv run python scripts/app_status.py                  # 켠 앱 모두, 지난 7일
    uv run python scripts/app_status.py --app grant-alerts --days 1 --json

DB는 마이그레이션 없이 읽기만 한다(머지 전 브랜치에서 돌려도 운영 DB의 스키마를 바꾸지 않는다).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
from noticedelta import app_registry  # noqa: E402
from noticedelta.app_status import app_status, describe  # noqa: E402
from noticedelta.config import database_url, load_env  # noqa: E402
from noticedelta.db import make_engine  # noqa: E402
from noticedelta.grants import APP  # noqa: E402

KST = timezone(timedelta(hours=9))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", action="append", help="앱 id(여러 번). 없으면 켠 앱 모두")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    load_env()
    factory = sessionmaker(make_engine(database_url()))
    now = datetime.now(UTC)
    today = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = int(os.environ.get("NOTICEDELTA_LLM_DAILY_TOKENS", "200000"))
    with factory() as s:
        apps = {a.id: a for a in app_registry.enabled(s)}
        apps.setdefault(APP.id, APP)
        for app_id in args.app or sorted(apps):
            app = apps.get(app_id)
            if app is None:
                print(f"{app_id}: 켠 판이 없습니다")
                continue
            st = app_status(s, app, root=ROOT / "data" / "snapshots",
                            since=now - timedelta(days=args.days), now=now, primary=APP.id,
                            daily=daily, today=today)  # fmt: skip
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
                continue
            print(f"## {st['app']} · 지난 {args.days}일")
            for line in describe(st):
                print(f"- {line}")
            print()


if __name__ == "__main__":
    main()
