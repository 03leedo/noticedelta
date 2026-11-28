"""키를 적기 전에 만든 판에 본문 조각의 원본 키를 채운다 (app-spec 9장, 마이그레이션 0023 뒤에 한 번).

    uv run python scripts/backfill_body_keys.py           # 세어 보기만(되돌린다)
    uv run python scripts/backfill_body_keys.py --apply   # 채운다

DB를 최신 스키마로 올린다(open_db). 머지한 뒤에 돌린다. 지금 어댑터로 원본을 다시 바꿔 본문이 글자까지
같은 판만 채우고, 저장된 본문 조각과 다시 나눈 조각이 같을 때만 조각에 키를 적는다(조각은 바꾸지 않는다).
"""

from __future__ import annotations

import argparse
import sys

from noticedelta import user_sources
from noticedelta.config import load_env, open_db
from noticedelta.ingest import backfill_body_keys
from noticedelta.programs import TO_RECORD


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="채운다(없으면 세어 보고 되돌린다)")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    with factory() as s:
        stats = backfill_body_keys(
            s, lambda source: TO_RECORD.get(source) or user_sources.to_record_for(s, source)
        )
        for k, v in sorted(stats.items()):
            print(f"{k}\t{v}")
        if args.apply:
            s.commit()
            print("채웠습니다")
        else:
            s.rollback()
            print("세어 보기만 했습니다(--apply로 채운다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
