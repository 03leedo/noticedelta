"""사업 버전마다 조각과 변경 이벤트를 남긴다 (작업 목록 11번).

    uv run python scripts/record_changes.py          # 버전이 둘 이상인 사업만
    uv run python scripts/record_changes.py --all    # 모든 사업 (첨부를 전부 파싱하므로 오래 걸린다)

먼저 load_snapshots.py로 스냅샷을 넣어 둔다. 아직 파싱하지 않은 첨부는 격리 파서로 파싱한다.
출력은 건수와 비율뿐이다(공고 내용은 찍지 않는다).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.db import make_engine, models  # noqa: E402
from noticedelta.history import record_program, replay  # noqa: E402
from noticedelta.ingest import parse_attachments  # noqa: E402
from noticedelta.parsing import DockerParser  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--all", action="store_true", help="버전이 하나인 사업도 기록")
    args = ap.parse_args()
    load_env(ROOT / ".env")
    engine = make_engine(os.environ.get("DATABASE_URL", "sqlite:///data/dev.db"))
    models.Base.metadata.create_all(engine)
    store = LocalBlobStore(Path(args.root) / "blobs")
    parser = DockerParser()

    with Session(engine) as session:
        q = sa.select(models.Program.id)
        if not args.all:
            q = q.where(models.Program.current_version_no >= 2)
        program_ids = session.scalars(q.order_by(models.Program.id)).all()
        statuses: dict[str, int] = {}
        for pid in program_ids:
            for pv in session.scalars(
                sa.select(models.ProgramVersion).where(models.ProgramVersion.program_id == pid)
            ):
                parse_attachments(session, pv.notice_version_id, parser=parser, store=store)
            for r in record_program(session, pid, store):
                statuses[r.status] = statuses.get(r.status, 0) + 1
            session.commit()

        ratios, sizes = [], []
        for pid in program_ids:
            for step in replay(session, pid):
                if step.from_version is None:
                    continue
                c = step.change
                # 다시 읽어야 할 조각(추가+수정) 비율. 전체 재추출(B2)은 늘 100%다.
                ratios.append((len(c.added) + len(c.modified)) / max(step.total, 1))
                sizes.append(step.total)
                print(
                    f"사업 {pid[:8]} v{step.from_version.version_no}→v{step.to_version.version_no}: "
                    f"조각 {step.total}, 추가 {len(c.added)}, 삭제 {len(c.removed)}, "
                    f"수정 {len(c.modified)}, 알 수 없음 {len(step.unknown)}"
                )
    print(f"사업 {len(program_ids)}개, 버전 기록 {statuses}")
    if ratios:
        print(
            f"변경 이벤트 {len(ratios)}건, 다시 읽을 조각 비율 중앙값 {statistics.median(ratios):.1%}"
            f" (최소 {min(ratios):.1%}, 최대 {max(ratios):.1%}), 버전당 조각 중앙값 "
            f"{statistics.median(sizes):.0f}"
        )


if __name__ == "__main__":
    main()
