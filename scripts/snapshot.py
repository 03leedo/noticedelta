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
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import database_url, load_env  # noqa: E402
from noticedelta.snapshot import PER_PAGE, run_snapshot  # noqa: E402
from noticedelta.sources import BizinfoClient, KStartupClient, RateLimiter  # noqa: E402
from noticedelta.sources.base import SourceError  # noqa: E402
from noticedelta.sources.html_list import decode, extract  # noqa: E402
from noticedelta.sources.html_list import to_record as list_record  # noqa: E402
from noticedelta.sources.rss import fetch_bytes, fetch_feed, to_record  # noqa: E402

OPERATOR = ("kstartup", "bizinfo")  # 운영자 출처(실패하면 알린다)

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


def user_feeds(http: httpx.Client) -> dict:
    """사용자가 등록한 RSS 출처의 받기 함수. DB는 마이그레이션 없이 읽기만 한다(없으면 빈 것)."""
    from sqlalchemy.orm import sessionmaker

    from noticedelta.db import make_engine
    from noticedelta.user_sources import sheets

    try:
        with sessionmaker(make_engine(database_url()))() as s:
            found = sheets(s)
    except Exception as e:  # DB가 없거나 열리지 않아도 운영자 출처는 받는다
        print(f"사용자 출처를 읽지 못했습니다: {type(e).__name__}", file=sys.stderr)
        return {}
    limiter = RateLimiter(1)  # 사용자 출처는 초당 1회를 넘지 않는다(source-catalog.md 6장)
    last = last_fetched(OUT / "runs", set(found))
    now = datetime.now()

    def feed(source_id: str, sheet):
        connector = sheet.connector
        board = connector.kind == "html_list"
        convert = list_record(source_id) if board else to_record(source_id)

        def page(n: int):
            if n > 1:
                return []
            limiter.acquire()
            if board:  # 게시판: 장에 적은 목록 위치로 글을 뽑는다
                data = fetch_bytes(connector.url, http)
                items = extract(decode(data), connector.url, connector.list)
            else:
                _, items = fetch_feed(connector.url, http)
            out = []
            for it in items:  # 변환하지 못하는 글 하나가 피드 전체를 막지 않게
                with contextlib.suppress(SourceError):
                    out.append(convert(it))
            return out

        return page

    due = {
        sid: sh
        for sid, sh in found.items()
        if sid not in last or now - last[sid] >= timedelta(hours=sh.fetch.every_hours)
    }  # 장에 적은 주기(every_hours)보다 자주 받지 않는다
    return {sid: feed(sid, sh) for sid, sh in due.items()}


def last_fetched(runs: Path, ids: set[str]) -> dict[str, datetime]:
    """출처마다 마지막으로 받은 실행의 시각(실행 파일 이름, 로컬 시각)."""
    out: dict[str, datetime] = {}
    for path in sorted(runs.glob("*.json"), reverse=True)[:200]:
        if not ids - set(out):
            break
        try:
            at = datetime.strptime(path.stem, "%Y%m%dT%H%M%S")
            sources = json.loads(path.read_text(encoding="utf-8")).get("sources", {})
        except (ValueError, OSError):
            continue
        for sid in ids & set(sources) - set(out):
            out[sid] = at
    return out


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
                    **user_feeds(http),
                },
                http=http,
                root=OUT,
                attachment_limiter=RateLimiter(1),
            )
    finally:
        LOCK.unlink(missing_ok=True)

    print(json.dumps(summary.sources, ensure_ascii=False, indent=2))
    print(f"변경·신규 {summary.new_or_changed}건, {summary.started_at} ~ {summary.finished_at}")
    failed = [k for k, v in summary.sources.items() if v.get("error")]
    operator = [k for k in failed if k in OPERATOR]
    users = [k for k in failed if k not in OPERATOR]
    if (
        users
    ):  # 사용자 출처의 실패는 운영자 알림·종료 코드에 넣지 않는다(상태 보고가 소유자에게 알린다)
        print(f"사용자 출처 실패: {', '.join(users)}", file=sys.stderr)
    if operator:
        alert(f"출처 실패: {', '.join(operator)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
