"""저장된 첨부를 격리 파서로 파싱하고 조각까지 만들어 본다 (작업 목록 4번 확인용).

    docker build -t noticedelta-parser infra/parser
    uv run python scripts/parse_live.py            # .env의 DATABASE_URL·원문 저장소 사용

출력은 건수·상태·사유와 조각 위치 예시뿐이다(공고 내용은 찍지 않는다).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta import blobstore  # noqa: E402
from noticedelta.db import make_engine, migrate, models  # noqa: E402
from noticedelta.ingest import parse_attachments, version_segments  # noqa: E402
from noticedelta.parsing import DockerParser, ParserUnavailable  # noqa: E402

NEW_COLUMNS = {"parser": "VARCHAR(64)", "parsed_key": "VARCHAR(64)", "review_reason": "TEXT"}


def add_missing_columns(engine: sa.Engine) -> None:
    """마이그레이션 도구를 붙이기 전까지, 예전에 만든 개발용 DB에 새 첨부 컬럼을 더한다."""
    have = {c["name"] for c in sa.inspect(engine).get_columns("attachment")}
    with engine.begin() as conn:
        for name, ddl in NEW_COLUMNS.items():
            if name not in have:
                conn.execute(sa.text(f"ALTER TABLE attachment ADD COLUMN {name} {ddl}"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="처리할 버전 수 (0이면 전부)")
    args = ap.parse_args()
    load_env(ROOT / ".env")
    (ROOT / "data").mkdir(exist_ok=True)
    engine = make_engine(os.environ.get("DATABASE_URL", "sqlite:///data/dev.db"))
    migrate.upgrade(engine)
    add_missing_columns(engine)
    store = blobstore.from_env()
    parser = DockerParser()

    statuses, reasons, parsers, kinds = Counter(), Counter(), Counter(), Counter()
    seg_total, example = 0, None
    with Session(engine) as session:
        versions = session.scalars(sa.select(models.NoticeVersion)).all()
        if args.limit:
            versions = versions[: args.limit]
        for i, v in enumerate(versions, 1):
            try:
                outcomes = parse_attachments(session, v.id, parser=parser, store=store)
            except ParserUnavailable as e:
                session.commit()
                print(f"\n파서를 돌릴 수 없어 멈춥니다(남은 첨부는 pending): {e}")
                print(
                    "Docker Desktop을 켜고 `docker build -t noticedelta-parser infra/parser` 뒤 다시 돌리세요"
                )
                return
            for o in outcomes:
                statuses[o.status] += 1
                if o.parser:
                    parsers[o.parser] += 1
                if o.reason:
                    reasons[o.reason.split(":")[0].split(" (")[0]] += 1
            session.commit()
            segs = version_segments(session, v, store)
            seg_total += len(segs)
            kinds.update(s.kind for s in segs)
            if example is None:
                example = next((s for s in segs if s.kind == "table_cell"), None)
            print(f"\r{i}/{len(versions)} 버전 처리", end="", flush=True)
    print()
    print("첨부 상태:", dict(statuses))
    print("파서:", dict(parsers))
    for r, n in reasons.most_common():
        print(f"  검토·제외 사유 {n}건: {r}")
    print(f"조각 {seg_total}개:", dict(kinds))
    if example:
        loc = {
            k: v for k, v in example.locator.items() if k not in ("row_key", "header", "table_sig")
        }
        print(
            "표 칸 조각 위치 예:", loc, "| 행 키·머리글 있음:", bool(example.locator.get("row_key"))
        )


if __name__ == "__main__":
    main()
