"""상시 수집 한 번 실행 (Windows 예약 작업이 부른다. scripts/install_snapshot_task.ps1 참고).

    uv run python scripts/snapshot.py            # data/snapshots에 쌓는다
    uv run python scripts/snapshot.py --status   # 마지막 실행 요약만 본다

- 인증키는 .env의 KSTARTUP_SERVICE_KEY, BIZINFO_SERVICE_KEY.
- 동시에 두 번 돌지 않게 잠금 파일을 쓴다(2시간 넘은 잠금은 죽은 것으로 보고 치운다).
- NOTICEDELTA_ALERT_WEBHOOK(디스코드 웹훅 등)이 있으면 실패했을 때만 알린다. 공고 내용은 보내지 않는다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env  # noqa: E402
from noticedelta.snapshot import PER_PAGE, run_snapshot  # noqa: E402
from noticedelta.sources import BizinfoClient, KStartupClient, RateLimiter  # noqa: E402

OUT = ROOT / "data" / "snapshots"
LOCK = OUT / ".lock"
STALE_LOCK_S = 2 * 3600


def acquire_lock() -> bool:
    OUT.mkdir(parents=True, exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < STALE_LOCK_S:
        return False
    LOCK.write_text(str(os.getpid()))
    return True


def alert(text: str) -> None:
    url = os.environ.get("NOTICEDELTA_ALERT_WEBHOOK")
    if not url:
        return
    with contextlib.suppress(httpx.HTTPError):  # 알림 실패가 수집 결과를 바꾸지 않게
        httpx.post(url, json={"content": f"[NoticeDelta 수집] {text}"}, timeout=10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        p = OUT / "last_run.json"
        print(p.read_text(encoding="utf-8") if p.exists() else "아직 실행 기록이 없습니다")
        return 0

    load_env()
    missing = [k for k in ("KSTARTUP_SERVICE_KEY", "BIZINFO_SERVICE_KEY") if not os.environ.get(k)]
    if missing:
        print(f".env에 {', '.join(missing)} 값이 없습니다", file=sys.stderr)
        alert(f"인증키 없음: {', '.join(missing)}")
        return 2
    if not acquire_lock():
        print("이전 실행이 아직 돌고 있어 건너뜁니다")
        return 0
    try:
        with httpx.Client(timeout=30) as http:
            ks = KStartupClient(os.environ["KSTARTUP_SERVICE_KEY"], http, limiter=RateLimiter(1))
            bz = BizinfoClient(os.environ["BIZINFO_SERVICE_KEY"], http, limiter=RateLimiter(1))
            summary = run_snapshot(
                {
                    "kstartup": lambda p: ks.list_notices(
                        page=p, per_page=PER_PAGE, only_open=True
                    ),
                    "bizinfo": lambda p: bz.list_notices(page=p, per_page=PER_PAGE),
                },
                http=http,
                root=OUT,
                attachment_limiter=RateLimiter(1),
            )
    finally:
        LOCK.unlink(missing_ok=True)

    print(json.dumps(summary.sources, ensure_ascii=False, indent=2))
    print(f"변경·신규 {summary.new_or_changed}건, {summary.started_at} ~ {summary.finished_at}")
    if not summary.ok:
        failed = [k for k, v in summary.sources.items() if v.get("error")]
        alert(f"출처 실패: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
