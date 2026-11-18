"""파이프라인 워커: 새 스냅샷 적재 → 첨부 파싱 → 변경 기록을 작업 큐로 돌린다 (작업 목록 13번).

    docker build -t noticedelta-parser infra/parser
    uv run python scripts/worker.py              # 할 일이 없을 때까지 처리하고 끝난다
    uv run python scripts/worker.py --status     # 작업 상태별 건수만 본다
    uv run python scripts/worker.py --backfill   # 변경 기록이 없는 사업 버전을 모두 등록

Docker가 꺼져 있으면 적재만 하고 끝낸다. 파싱·비교 작업은 큐에 남아 다음 실행에서 이어진다.

추출기(LLM)는 아직 붙이지 않았다. 그래서 변경 기록(diff)까지 돌고, 영향 계산은 등록하지 않는다.

DB는 .env의 DATABASE_URL(기본 sqlite:///data/dev.db). 아직 넣지 않은 실행이 있으면 적재 작업을
먼저 등록한다. 같은 작업은 한 번만 등록되므로 몇 번 돌려도 된다.
"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.drift import pending  # noqa: E402
from noticedelta.jobs import counts, run_worker  # noqa: E402
from noticedelta.parsing import DockerParser  # noqa: E402
from noticedelta.pipeline import (  # noqa: E402
    LOAD,
    Context,
    enqueue_backfill,
    enqueue_load,
    handlers,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--max-jobs", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="변경 기록이 없는 버전을 모두 등록")
    args = ap.parse_args()
    load_env()
    engine, factory = open_db()
    root = Path(args.root)

    with factory() as s:
        if not args.status:
            enqueue_load(s, root)
            if args.backfill:
                print(f"등록한 사업 버전: {enqueue_backfill(s)}")
            s.commit()
        print(f"작업 상태: {counts(s)}")
        held = pending(s)
        if held:
            # 출처가 형식을 바꾼 것으로 보여 정정으로 보지 않고 마지막 값을 이어 둔 키
            keys = ", ".join(f"{r.source}.{r.key}({r.kind} {r.changed}/{r.compared})" for r in held)
            print(f"형식 변화로 붙잡아 둔 키: {keys} · 결정은 scripts/drift.py")
    if args.status:
        return

    parser = DockerParser()
    ctx = Context(root=root, store=LocalBlobStore(root / "blobs"), parser=parser)
    todo = handlers(ctx)
    if not parser.available():
        # 파서 없이 파싱 작업을 꺼내면 재시도 횟수만 쓰고 실패로 굳는다. 적재만 하고 나머지는 둔다.
        print("파서(Docker)를 쓸 수 없어 적재만 합니다. 파싱·비교는 다음 실행에서 이어집니다")
        todo = {LOAD: todo[LOAD]}
    stats = run_worker(
        factory,
        todo,
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
        max_jobs=args.max_jobs,
    )
    with factory() as s:
        print(
            f"완료 {stats.done}, 재시도 예정 {stats.retried}, 실패 {stats.dead} · 작업 상태: {counts(s)}"
        )


if __name__ == "__main__":
    main()
