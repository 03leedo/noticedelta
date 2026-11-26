"""RQ6 파일럿을 돌린다: 도구 게이트웨이 대 효과마다 승인·샌드박스 단독 (noticedelta/gateway_pilot.py).

    uv run python scripts/rq6_pilot.py            # 승인자 정확도 1.0·0.9·0.7의 표
    uv run python scripts/rq6_pilot.py --trace    # 게이트웨이에서 과제마다 효과가 어떻게 됐는지

LLM을 쓰지 않는다. 임시 DB에서 돌고 운영 DB(data/dev.db)는 열지 않는다.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from noticedelta.gateway_pilot import gateway_trace, run

NAMES = {"sandbox": "샌드박스 단독", "per_effect": "효과마다 승인", "gateway": "게이트웨이"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()
    if args.trace:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            for r in gateway_trace(Path(tmp)):
                print(r)
        return
    print("| 승인자 정확도 | 정책 | 과제당 승인 요청 | 공격이 통한 효과 | 공격이 통한 과제 | 내 정보 샘 |"
          " 정상 효과 나감 | 정상인데 자유 글 빠짐 | 통했을 때 속성 |")  # fmt: skip
    print("|---|---|---|---|---|---|---|---|---|")
    for r in run():
        reached = ", ".join(f"{k or '없음'} {v}" for k, v in r["reached"].items() if v) or "-"
        print(f"| {r['acc']} | {NAMES[r['policy']]} | {r['asked_per_task']} | {r['harm']} |"
              f" {r['harm_tasks']} | {r['leaks']} | {r['benign_done']} | {r['benign_trimmed']} |"
              f" {reached} |")  # fmt: skip


if __name__ == "__main__":
    main()
