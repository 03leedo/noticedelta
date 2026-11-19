"""에이전트 사용 보고: 도구 호출과 피드백을 묶어 도구를 고칠 곳을 찾는다 (platform-services.md 10장).

    uv run python scripts/usage_report.py                  # 지난 7일
    uv run python scripts/usage_report.py --days 30
    uv run python scripts/usage_report.py --purge-days 180 # 보관 기한이 지난 기록을 지운다

결과 없는 검색어와 피드백은 개인정보를 가린 글만 보이고, 지시로 보이는 줄은 가린다.
보고서에 나온 피드백은 사람이 읽고 판단한다. 자동으로 적용하지 않는다.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.doc_tools import doc_tools  # noqa: E402
from noticedelta.item_tools import item_tools  # noqa: E402
from noticedelta.usage import purge, report  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--purge-days", type=int, help="이보다 오래된 사용 기록을 지운다")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    known = [t.name for t in [*doc_tools(None), *item_tools(factory)]] + ["send_feedback"]
    with factory() as s:
        if args.purge_days:
            print(f"지운 사용 기록 {purge(s, older_than_days=args.purge_days)}건")
            s.commit()
        since = datetime.now(UTC) - timedelta(days=args.days)
        print(report(s, since=since, known_tools=known))


if __name__ == "__main__":
    main()
