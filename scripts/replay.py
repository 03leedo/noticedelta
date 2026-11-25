"""같게 재생 (platform-services 3.4): 기록된 수집 실행을 빈 DB에서 처음부터 다시 돌려 출력을 비교한다.

    uv run python scripts/replay.py                       # 모든 실행
    uv run python scripts/replay.py --upto 20261117T103910  # 이 실행까지
    uv run python scripts/replay.py --twice               # 두 번 돌려 서로 같은지도 본다(결정성)

파서는 부르지 않고 원래 DB의 파싱 결과를 쓴다. 없으면 "기록 없음"으로 그 작업만 멈춘다.
원문 저장소는 읽기만 한다. 재생 DB는 data/replay/에 둔다(저장소에 올리지 않는다).
출력은 실행 이름, 봉투 수, 해시 앞자리와 비교 결과뿐이다.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.flight import platform_rev  # noqa: E402
from noticedelta.replay import compare, replay  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--upto", help="이 실행까지(이름, 예: 20261117T103910)")
    ap.add_argument("--work", default=str(ROOT / "data" / "replay"))
    ap.add_argument("--twice", action="store_true", help="두 번 돌려 해시가 같은지 본다")
    ap.add_argument("--show", type=int, default=5, help="다른 줄을 몇 개까지 보일까")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    root, work = Path(args.root), Path(args.work)
    store = LocalBlobStore(root / "blobs")

    t0 = time.monotonic()
    a = replay(factory, root, store, work / "a", upto=args.upto)
    print(
        f"플랫폼 판 {platform_rev()} · 실행 {len(a.runs)}개 · {time.monotonic() - t0:.0f}초 · 작업 {a.jobs}"
    )
    if a.missing:
        print(f"기록이 없어 멈춘 작업 {len(a.missing)}개. 예: {a.missing[0]}")
    b = None
    if args.twice:
        b = replay(factory, root, store, work / "b", upto=args.upto)
    print(
        "\n| 실행 | 봉투 | 해시 | 기록과 | 같은 판·설정 | 두 번째 재생과 |\n|---|---|---|---|---|---|"
    )
    for i, r in enumerate(a.runs):
        rec = "기록 없음" if r.same is None else ("같음" if r.same else "다름")
        again = "" if b is None else ("같음" if b.runs[i].digest == r.digest else "다름")
        print(
            f"| {r.run} | {len(r.lines)} | {(r.digest or '')[:12]} | {rec} | {'예' if r.comparable else '아니오'} | {again} |"
        )
    for r in a.runs:
        if r.same is False:
            only_rec, only_rep = compare(r.recorded_lines, r.lines)
            print(f"\n{r.run}: 기록에만 {len(only_rec)}줄, 재생에만 {len(only_rep)}줄")
            for line in only_rec[: args.show]:
                print(f"  - {line[:300]}")
            for line in only_rep[: args.show]:
                print(f"  + {line[:300]}")
    if b is not None:
        for x, y in zip(a.runs, b.runs, strict=True):
            if x.digest != y.digest:
                only_a, only_b = compare(x.lines, y.lines)
                print(f"\n{x.run}: 두 재생이 다름 (첫째에만 {len(only_a)}, 둘째에만 {len(only_b)})")


if __name__ == "__main__":
    main()
