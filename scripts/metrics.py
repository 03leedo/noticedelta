"""운영 지표를 JSON으로 찍는다 (작업 목록 20번). 측정표에 옮겨 쓰는 용도.

    uv run python scripts/metrics.py [--hours 24]

DB는 .env의 DATABASE_URL(기본 sqlite:///data/dev.db). 웹 서버의 /metrics는 같은 값을
Prometheus 형식으로 낸다(NOTICEDELTA_METRICS_TOKEN이 있을 때만).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta.db import make_engine, migrate  # noqa: E402
from noticedelta.metrics import collect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    args = ap.parse_args()
    load_env(ROOT / ".env")
    engine = make_engine(os.environ.get("DATABASE_URL", "sqlite:///data/dev.db"))
    migrate.upgrade(engine)
    with Session(engine) as s:
        m = collect(
            s, window=timedelta(hours=args.hours), snapshots_root=ROOT / "data" / "snapshots"
        )
    print(json.dumps(m, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
