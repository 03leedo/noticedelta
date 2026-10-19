"""실제 API에서 공고 몇 건을 받아 DB와 원문 저장소까지 기록해 본다 (수집 → 저장 연결 확인용).

    uv run --extra s3 --extra postgres python scripts/ingest_live.py --per-source 5

- 설정은 .env: 인증키, DATABASE_URL(기본 SQLite 파일), NOTICEDELTA_BLOB_BACKEND(local/s3).
- 첨부는 출처 서버에 부담을 주지 않게 초당 1건으로 받는다.
- 출력에는 건수·형식·실패 사유만 찍고 공고 내용은 찍지 않는다.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta import blobstore  # noqa: E402
from noticedelta.db import models  # noqa: E402
from noticedelta.ingest import ingest_record  # noqa: E402
from noticedelta.sources import BizinfoClient, CircuitBreaker, KStartupClient, RateLimiter  # noqa: E402


def ensure_bucket(store) -> None:
    if not isinstance(store, blobstore.S3BlobStore):
        return
    try:
        store.client.head_bucket(Bucket=store.bucket)
    except Exception:
        store.client.create_bucket(Bucket=store.bucket)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-source", type=int, default=5)
    args = ap.parse_args()
    load_env(ROOT / ".env")
    url = os.environ.get("DATABASE_URL", "sqlite:///data/dev.db")
    if url.startswith("sqlite:///data/"):
        (ROOT / "data").mkdir(exist_ok=True)
    engine = sa.create_engine(url)
    models.Base.metadata.create_all(engine)
    store = blobstore.from_env()
    ensure_bucket(store)
    print(f"DB: {engine.url.render_as_string(hide_password=True)}  저장소: {type(store).__name__}")

    with httpx.Client(timeout=30) as http, Session(engine) as session:
        sources = [
            KStartupClient(os.environ["KSTARTUP_SERVICE_KEY"], http, limiter=RateLimiter(1)),
            BizinfoClient(os.environ["BIZINFO_SERVICE_KEY"], http, limiter=RateLimiter(1)),
        ]
        for client in sources:
            recs = client.list_notices(page=1, per_page=args.per_source)
            att_limiter, breaker = RateLimiter(1), CircuitBreaker(failure_threshold=3)
            versions = stored = 0
            mimes, fails = Counter(), Counter()
            for rec in recs:
                res = ingest_record(
                    session, rec, http=http, store=store, limiter=att_limiter, breaker=breaker
                )
                versions += res.version is not None
                stored += len(res.stored)
                mimes.update(s.mime for s in res.stored)
                fails.update(reason for _, reason in res.failed)
            session.commit()
            name = type(client).__name__
            print(f"[{name}] 공고 {len(recs)}건, 새 버전 {versions}건, 첨부 저장 {stored}건")
            for mime, n in mimes.most_common():
                print(f"    {mime}: {n}")
            for reason, n in fails.most_common():
                print(f"    실패 {n}건: {reason}")

        totals = {
            t.__tablename__: session.scalar(sa.select(sa.func.count()).select_from(t))
            for t in (models.Notice, models.NoticeVersion, models.Attachment)
        }
        print("DB 행 수:", totals)


if __name__ == "__main__":
    main()
