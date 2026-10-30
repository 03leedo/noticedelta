"""상시 수집 스냅샷(data/snapshots)을 DB에 넣고 게시물을 사업으로 묶는다.

    uv run python scripts/load_snapshots.py [--root data/snapshots]

DB는 .env의 DATABASE_URL(기본 sqlite:///data/dev.db). 이미 넣은 실행은 건너뛴다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.db import make_engine, migrate  # noqa: E402
from noticedelta.programs import load_snapshots  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "snapshots"))
    args = ap.parse_args()
    load_env(ROOT / ".env")
    (ROOT / "data").mkdir(exist_ok=True)
    engine = make_engine(os.environ.get("DATABASE_URL", "sqlite:///data/dev.db"))
    migrate.upgrade(engine)
    root = Path(args.root)
    with Session(engine) as session:
        s = load_snapshots(session, root, LocalBlobStore(root / "blobs"))
    print(f"실행 {s.runs}개, 항목 {s.items}개")
    print(f"새 게시물 {s.new_postings}, 게시물 버전 {s.new_posting_versions}, 닫힘 {s.closed}")
    print(f"새 사업 {s.new_programs}, 사업 버전 {s.new_program_versions}, 연결 {dict(s.links)}")


if __name__ == "__main__":
    main()
