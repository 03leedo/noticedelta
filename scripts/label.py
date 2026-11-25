"""정답 라벨 화면을 띄운다 (작업 목록 5a). 내 PC에서만(127.0.0.1).

    uv sync --extra web
    uv run python scripts/label.py              # http://127.0.0.1:8001
    uv run python scripts/label.py --focus data/labels/focus-dev.json   # 이 순서로 먼저 (기본값)

순서 파일은 scripts/eval.py --focus-out 이 만든다(방식끼리 갈린 정정 먼저, 필드와 값은 없음).

DB는 .env의 DATABASE_URL(기본 sqlite:///data/dev.db), 첨부 원본은 data/snapshots/blobs.
지침: docs/labeling.md. 엔진 결과를 보기 전에 라벨한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.labeling.app import create_label_app  # noqa: E402
from noticedelta.labeling.store import load_focus  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--labeler", default="me", help="두 번째 사람이 라벨할 때 다른 이름")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--focus", type=Path, default=ROOT / "data" / "labels" / "focus-dev.json")
    args = ap.parse_args()
    load_env()
    engine, factory = open_db()
    app = create_label_app(
        factory,
        LocalBlobStore(Path(args.root) / "blobs"),
        labeler=args.labeler,
        focus=load_focus(args.focus),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
