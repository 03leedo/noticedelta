"""하네스를 실제로 모은 공고 조각에 돌려 본다 (LLM 호출 없음, ARCHITECTURE RQ9의 바탕).

    uv run python scripts/harness_scan.py            # 건수만
    uv run python scripts/harness_scan.py --show 20  # 인젝션 의심 줄을 20개까지 보여 준다(로컬 확인용)

- 개인정보 가리기: 종류별로 가려지는 건수, 가렸다가 되돌리면 원문과 같은지(전수 확인)
- 인젝션 탐지: 실제 공고에서 의심으로 잡히는 줄 수. 실제 공고에는 심은 지시가 없다고 보면 이것이 오탐이다

출력은 건수뿐이다. --show는 공고 문장을 찍으므로 결과를 저장소에 올리지 않는다.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.db.models import SegmentRow  # noqa: E402
from noticedelta.llm.harness import INJECTION_RULES, mask, scan, unmask  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0, help="의심 줄을 이만큼 보여 준다")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()

    rows = distinct = with_pii = broken = flagged = 0
    pii, rules, single = Counter(), Counter(), Counter()
    seen: set[str] = set()
    shown = []
    with factory() as s:
        q = sa.select(SegmentRow.text_hash, SegmentRow.text).execution_options(yield_per=5000)
        for text_hash, text in s.execute(q):
            rows += 1
            if text_hash in seen:
                continue
            seen.add(text_hash)
            distinct += 1
            masked, table = mask(text)
            if table:
                with_pii += 1
                for placeholder in table:
                    pii[placeholder[2:].split("_")[0]] += 1
                if unmask(masked, table) != text:
                    broken += 1
            for line in text.split("\n"):
                hit = scan(line)
                if hit:
                    flagged += 1
                    rules.update(hit)
                    if len(shown) < args.show:
                        shown.append((hit, line[:100]))
                else:  # 규칙 하나만 걸린 줄(의심으로 치지 않는다). 규칙이 얼마나 흔한지 본다
                    single.update(n for n, _, p in INJECTION_RULES if p.search(line))

    print(f"조각 {rows:,}개 (서로 다른 글 {distinct:,}개)")
    print(
        f"개인정보가 든 조각 {with_pii:,}개 ({with_pii / max(distinct, 1):.1%}), 종류별 가린 수: {dict(pii)}"
    )
    print(f"가렸다가 되돌렸을 때 원문과 다른 조각: {broken}개")
    print(f"인젝션 의심 줄 {flagged}개 ({flagged / max(distinct, 1):.4%}), 규칙별: {dict(rules)}")
    print(f"규칙 하나만 걸려 넘어간 줄: {dict(single)}")
    for hit, line in shown:
        print("  ", hit, "|", line)


if __name__ == "__main__":
    main()
