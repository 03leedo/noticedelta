"""저장된 원문(data/snapshots)에서 DB를 처음부터 다시 만든다.

정규화·묶기 규칙을 바꿨을 때 쓴다. 수집한 원문은 그대로 두고, 거기서 나온 것(게시물·사업 버전,
조각, 변경 이벤트)만 지금 규칙으로 다시 계산한다. 서버에 처음 올릴 때 하는 일과 같다.

    uv run python scripts/rebuild.py            # 옛 DB를 백업 이름으로 옮기고 새로 만든다 (SQLite)
    uv run python scripts/rebuild.py --reparse  # 첨부 파싱 결과도 버리고 다시 파싱한다

- 첨부 파싱 결과는 옛 DB에서 옮겨 온다. 같은 파일은 다시 파싱하지 않으므로 빨리 끝난다.
- 라벨이나 프로필이 있으면 멈춘다. 사업 버전 ID가 새로 생겨 라벨이 끊기기 때문이다(--force로 무시).
- PostgreSQL은 DB를 새로 만든 뒤 scripts/worker.py를 돌리면 된다(docs/deploy.md).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import blob_store, database_url, load_env, open_db, snapshots_root  # noqa: E402
from noticedelta.db.models import Attachment, LabelRow, ParseCache, Profile  # noqa: E402
from noticedelta.jobs import counts, run_worker  # noqa: E402
from noticedelta.parsing import DockerParser  # noqa: E402
from noticedelta.pipeline import Context, enqueue_load, handlers  # noqa: E402


def parse_results(session, store) -> list[dict]:
    """옛 DB에서 파일(내용 해시)별 파싱 결과를 모은다. 실패로 끝난 것은 옮기지 않는다."""
    out = {(c.blob_key, c.mime): c for c in session.scalars(sa.select(ParseCache))}
    rows = session.scalars(
        sa.select(Attachment).where(
            Attachment.blob_key.is_not(None),
            Attachment.parsed_key.is_not(None),
            Attachment.parse_status.in_(("ok", "needs_review")),
        )
    )
    for att in rows:
        key = (att.blob_key, att.mime)
        if key in out:
            continue
        # 시간 초과 같은 실패로 확인 필요가 된 것은 다시 파싱해 볼 수 있게 옮기지 않는다
        failed = (
            att.parse_status == "needs_review"
            and json.loads(store.get(att.parsed_key)).get("status") != "ok"
        )
        if failed:
            continue
        out[key] = att
    return [
        {
            "blob_key": r.blob_key,
            "mime": r.mime,
            "parse_status": r.parse_status,
            "parser": r.parser,
            "parsed_key": r.parsed_key,
            "review_reason": r.review_reason,
            "segments": getattr(r, "segments", 0) or 0,
        }
        for r in out.values()
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reparse", action="store_true", help="첨부 파싱 결과를 옮기지 않는다")
    ap.add_argument("--force", action="store_true", help="라벨·프로필이 있어도 진행한다")
    args = ap.parse_args()
    load_env()
    url = database_url()
    if not url.startswith("sqlite:///"):
        sys.exit("SQLite에서만 씁니다. PostgreSQL은 DB를 새로 만들고 scripts/worker.py를 돌리세요")
    path = Path(url.removeprefix("sqlite:///"))
    parser = DockerParser()
    if not parser.available():
        sys.exit(
            "파서(Docker)를 쓸 수 없습니다. 처음 보는 파일을 파싱할 수 없으니 Docker를 켜고 다시 돌리세요"
        )

    root, store = snapshots_root(), blob_store()
    engine, factory = open_db()
    with factory() as s:
        kept = {
            m.__tablename__: s.scalar(sa.select(sa.func.count()).select_from(m))
            for m in (LabelRow, Profile)
        }
        if any(kept.values()) and not args.force:
            sys.exit(f"라벨·프로필이 있어 멈춥니다: {kept}. 다시 만들면 끊깁니다(--force로 진행)")
        cache = [] if args.reparse else parse_results(s, store)
    engine.dispose()

    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M}-rebuild")
    path.rename(backup)
    print(f"옛 DB: {backup.name} · 옮겨 온 파싱 결과 {len(cache)}개")

    engine, factory = open_db()
    with factory() as s:
        if cache:
            s.execute(sa.insert(ParseCache), cache)
        enqueue_load(s, root)
        s.commit()
    ctx = Context(root=root, store=store, parser=parser)
    stats = run_worker(factory, handlers(ctx), worker_id=f"{socket.gethostname()}:{os.getpid()}")
    with factory() as s:
        print(
            f"완료 {stats.done}, 재시도 예정 {stats.retried}, 실패 {stats.dead} · 작업 상태: {counts(s)}"
        )


if __name__ == "__main__":
    main()
