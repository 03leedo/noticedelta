"""보관 기한 정리 (noticedelta/retention.py): 기한이 지난 금고 값, 오래된 허가, 끝난 효과의 인자를 지운다.

    uv run python scripts/retention.py              # 무엇을 지울지 세기만 한다(아무것도 바꾸지 않는다)
    uv run python scripts/retention.py --apply      # 지운다
    uv run python scripts/retention.py --apply --end-beta   # 베타가 끝났으면(KEEP_UNTIL이 지났으면) 모두

--end-beta는 web/consent.py의 KEEP_UNTIL이 지난 뒤에만 사람의 정보를 지운다. 그 전에는 아무것도 하지 않는다.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.retention import purge  # noqa: E402
from noticedelta.web.consent import KEEP_UNTIL  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 지운다(없으면 세기만)")
    ap.add_argument("--end-beta", action="store_true", help="베타가 끝났으면 사람의 정보도 모두")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    with factory() as s:
        p = purge(s, now=datetime.now(UTC), beta_ends=date.fromisoformat(KEEP_UNTIL),
                  end_beta=args.end_beta)  # fmt: skip
        verb = "지웠습니다" if args.apply else "지울 것입니다(--apply로 지웁니다)"
        print(
            f"기한 지난 금고 값 {p.vault_values}개, 오래된 허가 {p.grants}개, "
            f"끝난 효과의 인자 {p.actions}개, 베타 종료로 지운 사람 {p.people}명을 {verb}"
        )
        if args.apply:
            s.commit()
        else:
            s.rollback()


if __name__ == "__main__":
    main()
