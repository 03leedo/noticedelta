"""설정: .env 읽기와 DB·원문 저장소 열기. 스크립트가 모두 같은 방법으로 시작하게 한다.

DATABASE_URL           기본 sqlite:///data/dev.db (저장소 폴더 기준)
NOTICEDELTA_SNAPSHOTS  기본 data/snapshots (수집 실행 기록과 원문 blobs/)
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from noticedelta.blobstore import LocalBlobStore

ROOT = Path(__file__).resolve().parents[2]  # 저장소 폴더 (src/noticedelta/config.py 기준)
DEFAULT_DB = "sqlite:///data/dev.db"


def load_env(path: Path | None = None) -> None:
    """KEY=VALUE 줄을 환경 변수로. 이미 있는 값과 빈 값은 건드리지 않는다."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if v.strip():
            os.environ.setdefault(k.strip(), v.strip())


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DB)


def snapshots_root() -> Path:
    return Path(os.environ.get("NOTICEDELTA_SNAPSHOTS", ROOT / "data" / "snapshots"))


def blob_store(root: Path | None = None) -> LocalBlobStore:
    return LocalBlobStore((root or snapshots_root()) / "blobs")


def open_db(url: str | None = None, **engine_kwargs) -> tuple[sa.Engine, sessionmaker[Session]]:
    """DB를 열고 마이그레이션으로 최신 스키마까지 올린다."""
    from noticedelta.db import make_engine, migrate

    (ROOT / "data").mkdir(exist_ok=True)
    engine = make_engine(url or database_url(), **engine_kwargs)
    migrate.upgrade(engine)
    return engine, sessionmaker(engine)
