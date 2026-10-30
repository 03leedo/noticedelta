"""판정 화면을 띄운다 (작업 목록 7번).

    uv sync --extra web
    uv run python scripts/web.py --demo     # data/demo.db에 가상 사업 셋을 넣고 띄운다
    uv run python scripts/web.py            # .env의 DATABASE_URL(기본 sqlite:///data/dev.db)

http://127.0.0.1:8000 에서 조건을 넣으면 개인 링크가 생긴다. 실제 공고는 AI 추출(5번)을 붙인 뒤에
자격 규칙이 생기므로, 그 전에는 --demo로 확인한다.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import uvicorn
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta.db import make_engine, migrate  # noqa: E402
from noticedelta.web.app import create_app  # noqa: E402
from noticedelta.web.demo import seed_demo  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="가상 사업이 든 data/demo.db로 띄운다")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    load_env(ROOT / ".env")
    (ROOT / "data").mkdir(exist_ok=True)
    url = (
        f"sqlite:///{ROOT / 'data' / 'demo.db'}"
        if args.demo
        else os.environ.get("DATABASE_URL", "sqlite:///data/dev.db")
    )
    engine = make_engine(url)
    migrate.upgrade(engine)
    factory = sessionmaker(engine)
    if args.demo:
        with factory() as s:
            print(f"가상 사업 {seed_demo(s, date.today())}개를 넣었습니다")
            s.commit()
    app = create_app(factory, metrics_token=os.environ.get("NOTICEDELTA_METRICS_TOKEN"))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
