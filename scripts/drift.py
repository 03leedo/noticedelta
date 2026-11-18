"""출처 형식 변화로 붙잡아 둔 키를 보고 결정한다 (source-catalog.md 5장, drift.py).

    uv run python scripts/drift.py                                   # 붙잡은 키와 최근 기록
    uv run python scripts/drift.py --ignore kstartup.some_key        # 이 키의 변화는 보지 않는다
    uv run python scripts/drift.py --release kstartup.some_key       # 진짜 변경으로 받는다(다음 적재 한 번)

붙잡은 키는 적재할 때마다 마지막으로 본 값을 이어 둔다. 값이 대부분 돌아오면 저절로 풀린다.
출력은 키 이름과 건수, 외부 ID뿐이다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
from noticedelta import drift  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.db.models import SourceDrift  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ignore", metavar="출처.키")
    ap.add_argument("--release", metavar="출처.키")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    with factory() as s:
        for target, status in ((args.ignore, drift.IGNORED), (args.release, drift.RELEASED)):
            if not target:
                continue
            source, _, key = target.partition(".")
            try:
                drift.decide(s, source, key, status, args.note)
            except (LookupError, ValueError) as e:
                sys.exit(str(e))
            s.commit()
            print(f"{source}.{key}: {status}")
        rows = s.scalars(sa.select(SourceDrift).order_by(SourceDrift.id.desc()).limit(20)).all()
        if not rows:
            print("붙잡은 키가 없습니다")
        for r in rows:
            hint = ""
            if (
                r.status == drift.HELD
                and r.kind == "changed"
                and r.runs >= drift.SUSPECT_VOLATILE_RUNS
            ):
                hint = " · 여러 번 이어졌다: 호출마다 바뀌는 값이면 --ignore"
            print(
                f"[{r.status}] {r.source}.{r.key} {r.kind} {r.changed}/{r.compared} "
                f"· {r.first_run}~{r.last_run} {r.runs}회 · 예: {', '.join(r.samples)}{hint}"
            )


if __name__ == "__main__":
    main()
