"""두 출처의 열린 공고 전체를 주기적으로 스냅샷한다 (상시 수집, 작업 목록 0번).

K-Startup은 같은 공고 번호를 그 자리에서 고친다. 오늘 저장하지 않은 "변경 전" 상태는 나중에
다시 구할 수 없으므로, 이 수집기는 DB·Docker 없이 파일만으로 돈다. 사업(program) 모델과
PostgreSQL 작업 테이블이 붙으면 이 기록을 그대로 다시 넣는다.

저장 형태 (root = data/snapshots):
- blobs/      원문 저장소. 공고 항목 JSON(내용과 무관한 값은 뺀 정규형)과 첨부 원본, 내용 해시 키
- runs/<실행 시각>.json   실행 한 번의 목록: 출처·외부 ID·내용 해시·첨부 키, 출처별 오류
- attachments.jsonl       첨부 받기 기록 (url, 공고 내용 해시, 결과). 같은 공고 내용이면 다시 받지 않는다
- last_run.json           마지막 실행 요약 (생존 확인용)

첨부는 공고 내용 해시가 처음 보이거나 바뀐 경우에만 받는다. 같은 URL의 첨부 내용만 바뀌는 경우는
주기적 재확인(나중 작업)으로 잡는다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from noticedelta.blobstore import BlobStore, LocalBlobStore
from noticedelta.sources import NoticeRecord, SourceError, TransientSourceError
from noticedelta.sources.attachments import download_attachment
from noticedelta.sources.base import _VOLATILE
from noticedelta.sources.resilience import CircuitBreaker, CircuitOpen, RateLimiter

PER_PAGE = 100
MAX_PAGES = 50  # 출처가 이상한 응답을 줘도 무한히 돌지 않게


@dataclass
class SourceResult:
    source: str
    items: list[dict] = field(default_factory=list)
    error: str | None = None
    attachments_fetched: int = 0
    attachment_errors: int = 0


@dataclass
class RunSummary:
    started_at: str
    finished_at: str
    sources: dict[str, dict]
    new_or_changed: int
    ok: bool


def canonical_item(raw: dict) -> bytes:
    """내용과 무관한 값(조회수, 행 번호, 일괄 수정 시각)을 뺀 정규 JSON."""
    kept = {k: v for k, v in raw.items() if k not in _VOLATILE}
    return json.dumps(kept, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def fetch_all(list_page: Callable[[int], list[NoticeRecord]]) -> list[NoticeRecord]:
    out: list[NoticeRecord] = []
    for page in range(1, MAX_PAGES + 1):
        recs = list_page(page)
        out += recs
        if len(recs) < PER_PAGE:
            break
    return out


class AttachmentIndex:
    """(첨부 URL, 공고 내용 해시) → 받은 결과. 줄 단위로 덧붙이기만 한다."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[tuple[str, str], dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.seen[(r["url"], r["content_hash"])] = r

    def get(self, url: str, content_hash: str) -> dict | None:
        r = self.seen.get((url, content_hash))
        return r if r and r.get("blob_key") else None  # 실패 기록은 다음 실행에서 다시 시도

    def add(self, record: dict) -> None:
        self.seen[(record["url"], record["content_hash"])] = record
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def snapshot_source(
    name: str,
    records: Iterable[NoticeRecord],
    *,
    http: httpx.Client,
    store: BlobStore,
    index: AttachmentIndex,
    previous: dict[str, str],
    limiter: RateLimiter | None = None,
    breaker: CircuitBreaker | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> SourceResult:
    result = SourceResult(name)
    for rec in records:
        item_key = store.put(canonical_item(rec.raw), content_type="application/json").key
        atts = []
        for ref in rec.attachments:
            hit = index.get(ref.url, rec.content_hash)
            entry = {"filename": ref.filename, "url": ref.url, "role": ref.role}
            if hit is None and breaker is not None and not breaker.allow():
                entry["error"] = "서킷 열림"
            elif hit is None:
                record = {
                    "url": ref.url,
                    "content_hash": rec.content_hash,
                    "at": now().isoformat(timespec="seconds"),
                }
                try:
                    stored = download_attachment(
                        http, ref, store, source=name, limiter=limiter, breaker=breaker
                    )
                    record.update(blob_key=stored.blob_key, mime=stored.mime, size=stored.size)
                    result.attachments_fetched += 1
                except (SourceError, CircuitOpen, httpx.InvalidURL, ValueError) as e:
                    record["error"] = str(e) or type(e).__name__
                    result.attachment_errors += 1
                index.add(record)
                hit = record if record.get("blob_key") else None
                if hit is None:
                    entry["error"] = record["error"]
            if hit is not None:
                entry.update(blob_key=hit["blob_key"], mime=hit.get("mime"))
            atts.append(entry)
        result.items.append(
            {
                "external_id": rec.external_id,
                "title": rec.title,
                "content_hash": rec.content_hash,
                "item_key": item_key,
                "changed": previous.get(rec.external_id) != rec.content_hash,
                "updated_at": rec.raw.get("updtPnttm"),
                "attachments": atts,
            }
        )
    return result


def last_hashes(runs_dir: Path) -> dict[str, dict[str, str]]:
    """출처별로 가장 최근에 성공한 실행의 {외부 ID: 내용 해시}.

    마지막 실행에서 그 출처가 실패했으면(절전에서 깬 직후 네트워크 오류 등) 그 전 실행을 본다.
    실패한 실행을 기준으로 삼으면 다음 실행이 모든 공고를 "새 공고"로 센다.
    """
    out: dict[str, dict[str, str]] = {}
    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        for src, body in data["sources"].items():
            if src not in out and body.get("error") is None:
                out[src] = {it["external_id"]: it["content_hash"] for it in body.get("items", [])}
    return out


# 잠깐의 연결 실패(절전에서 깬 직후, 포털 일시 장애)는 몇 분 기다렸다 다시 받는다. 합 7.5분.
RETRY_DELAYS_S = (30, 60, 120, 240)


def fetch_with_retry(
    list_page: Callable[[int], list[NoticeRecord]],
    *,
    delays: Iterable[float] = RETRY_DELAYS_S,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[NoticeRecord], int]:
    """(공고 목록, 시도 횟수). 일시 오류가 아니거나 재시도가 끝나면 마지막 오류를 올린다."""
    waits = list(delays)
    attempt = 0
    while True:
        attempt += 1
        try:
            return fetch_all(list_page), attempt
        except TransientSourceError:
            if attempt > len(waits):
                raise
            sleep(waits[attempt - 1])


def run_snapshot(
    sources: dict[str, Callable[[int], list[NoticeRecord]]],
    *,
    http: httpx.Client,
    root: Path,
    store: BlobStore | None = None,
    attachment_limiter: RateLimiter | None = None,
    now: Callable[[], datetime] = datetime.now,
    retry_delays: Iterable[float] = RETRY_DELAYS_S,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    root.mkdir(parents=True, exist_ok=True)
    runs_dir = root / "runs"
    runs_dir.mkdir(exist_ok=True)
    store = store or LocalBlobStore(root / "blobs")
    index = AttachmentIndex(root / "attachments.jsonl")
    previous = last_hashes(runs_dir)
    started = now()

    bodies: dict[str, dict] = {}
    for name, list_page in sources.items():
        try:
            records, attempts = fetch_with_retry(list_page, delays=retry_delays, sleep=sleep)
        except (SourceError, CircuitOpen) as e:
            # 출처 하나가 실패해도 다른 출처는 계속 받는다. 실패는 실행 기록에 남긴다.
            bodies[name] = {"error": str(e), "items": []}
            continue
        res = snapshot_source(
            name,
            records,
            http=http,
            store=store,
            index=index,
            previous=previous.get(name, {}),
            limiter=attachment_limiter,
            breaker=CircuitBreaker(failure_threshold=5),
            now=now,
        )
        bodies[name] = {
            "error": None,
            "attempts": attempts,
            "count": len(res.items),
            "changed": sum(it["changed"] for it in res.items),
            "attachments_fetched": res.attachments_fetched,
            "attachment_errors": res.attachment_errors,
            "items": res.items,
        }

    finished = now()
    stamp = started.strftime("%Y%m%dT%H%M%S")
    run = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "sources": bodies,
    }
    tmp = runs_dir / f".{stamp}.tmp"
    tmp.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    tmp.replace(runs_dir / f"{stamp}.json")

    summary = RunSummary(
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        sources={k: {kk: vv for kk, vv in v.items() if kk != "items"} for k, v in bodies.items()},
        new_or_changed=sum(v.get("changed", 0) for v in bodies.values()),
        ok=all(v["error"] is None for v in bodies.values()),
    )
    (root / "last_run.json").write_text(
        json.dumps(summary.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
