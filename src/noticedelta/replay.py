"""같게 재생 (platform-services 3.4): 기록된 입력으로 빈 DB에서 수집 실행들을 처음부터 다시 돌린다.

결정적이지 않은 경계는 기록된 것으로 바꾼다.
- 원문: 실행 파일(runs/)과 원문 저장소(blobs/). 저장소는 읽기만 한다. 없는 것을 쓰려 하면 멈춘다.
- 파서: 원래 DB의 파싱 결과(parse_cache, 첨부의 parsed_key). 없으면 "기록 없음"으로 멈춘다.
- LLM: 녹화본(REPLAY 모드 게이트웨이). 운영 워커가 추출기를 붙이지 않았으므로 기본은 없다.
- 사람의 판단: 형식 변화 결정(source_drift). 결정한 뒤의 실행부터 같은 결정을 적용한다.
- 시계: 실행마다 그 실행이 끝난 시각에서 시작한다. 기다리는 작업이 있으면 다음 실행 전까지 앞으로 감는다.
  작업 큐도 이 시계로 등록한다(jobs.enqueue의 now). 시각은 해시에 넣지 않는다.

출력은 수집 실행 묶음마다 봉투의 정규형(flight.canonical_lines)과 그 해시다. 같은 코드·설정으로
기록된 묶음의 해시와 다르면 결정적이지 않은 것이 새는 것이다(버그). 판이 다르면 바꿔 재생이다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from noticedelta import drift, flight
from noticedelta.appspec import AppDefinition
from noticedelta.blobstore import BlobRef, BlobStore, blob_key
from noticedelta.db import make_engine, models
from noticedelta.db.models import Attachment, EventRow, Job, ParseCache, SourceDrift, TraceRow
from noticedelta.grants import APP
from noticedelta.impact import Extractor
from noticedelta.jobs import PermanentError, run_worker
from noticedelta.parsing import ParsedDocument
from noticedelta.pipeline import Context, enqueue_load, handlers

RECORDED_PARSER = "recorded"


class MissingRecording(PermanentError):
    """재생에 필요한 기록이 없다(평가 틀의 '녹화본 없음'과 같다). 그 작업은 다시 시도하지 않는다."""


class RecordedParser:
    """파서 대신 원래 DB에 남은 파싱 결과를 준다."""

    version = RECORDED_PARSER

    def __init__(self, parses: dict[tuple[str, str], str], store: BlobStore):
        self.parses, self.store = parses, store

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        key = blob_key(data)
        parsed = self.parses.get((key, mime))
        if parsed is None:
            raise MissingRecording(f"파싱 기록 없음: {key[:12]} {mime}")
        return ParsedDocument.from_json(json.loads(self.store.get(parsed)))


class ReadOnlyStore:
    """원문 저장소를 읽기만 한다. 이미 있는 것을 다시 쓰는 것(내용 주소라 같은 키)은 허용한다."""

    def __init__(self, inner: BlobStore):
        self.inner = inner

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def exists(self, key: str) -> bool:
        return self.inner.exists(key)

    def put(self, data: bytes, content_type: str | None = None) -> BlobRef:
        key = blob_key(data)
        if not self.inner.exists(key):
            raise MissingRecording(f"원문 저장소에 없는 것을 쓰려 함: {key[:12]}")
        return BlobRef(key, len(data), created=False)


@dataclass(frozen=True)
class Decision:
    at: datetime
    source: str
    key: str
    status: str


def recorded_parses(session: Session) -> dict[tuple[str, str], str]:
    """(원문 키, 형식) → 파싱 결과 키. 캐시가 먼저고, 캐시에 남지 않은 실패도 첨부에서 찾는다."""
    out = {
        (r.blob_key, r.mime): r.parsed_key
        for r in session.scalars(sa.select(ParseCache).where(ParseCache.parsed_key.is_not(None)))
    }
    for a in session.scalars(
        sa.select(Attachment).where(
            Attachment.parsed_key.is_not(None), Attachment.blob_key.is_not(None)
        )
    ):
        out.setdefault((a.blob_key, a.mime), a.parsed_key)
    return out


def recorded_decisions(session: Session) -> list[Decision]:
    rows = session.scalars(
        sa.select(SourceDrift).where(
            SourceDrift.decided_at.is_not(None),
            SourceDrift.status.in_((drift.IGNORED, drift.RELEASED)),
        )
    )
    return sorted(
        (Decision(_aware(r.decided_at), r.source, r.key, r.status) for r in rows),
        key=lambda d: d.at,
    )


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def run_time(name: str, body: dict) -> datetime:
    """실행이 끝난 시각. 파일에 없으면 이름(20261117T103910)에서 읽는다."""
    for k in ("finished_at", "started_at"):
        if body.get(k):
            try:
                return _aware(datetime.fromisoformat(body[k]))
            except ValueError:
                pass
    return datetime.strptime(name, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2000, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@dataclass
class RunResult:
    run: str
    trace_id: str | None
    digest: str | None
    lines: list[str] = field(default_factory=list)
    recorded: TraceRow | None = None  # 원래 DB에 기록된 같은 실행의 묶음
    recorded_lines: list[str] = field(default_factory=list)  # 그 묶음의 정규형(원래 DB에서)

    @property
    def comparable(self) -> bool:
        """같은 코드·설정으로 기록된 묶음인가(파서는 기록으로 바꿨으므로 빼고 본다)."""
        r = self.recorded
        return (
            r is not None
            and r.digest is not None
            and r.platform_rev == flight.platform_rev()
            and _without_parser(r.config) == self.config
        )

    config: dict = field(default_factory=dict)

    @property
    def same(self) -> bool | None:
        if self.recorded is None or self.recorded.digest is None:
            return None
        return self.recorded.digest == self.digest


def _without_parser(config: dict) -> dict:
    return {k: v for k, v in config.items() if k != "parser"}


@dataclass
class Replay:
    runs: list[RunResult]
    missing: list[str]  # 기록이 없어 멈춘 작업의 이유
    jobs: dict[str, int]
    db: Path


def _drain(
    factory: Callable[[], Session], ctx: Context, clock: Clock, until: datetime | None
) -> None:
    """할 일이 없을 때까지 돌리고, 기다리는 작업이 다음 실행 전에 풀리면 시계를 감아 이어 돌린다."""
    while True:
        run_worker(factory, handlers(ctx), worker_id="replay", now=clock)
        with factory() as s:
            nxt = s.scalar(
                sa.select(sa.func.min(Job.run_after)).where(Job.status.in_(("queued", "retry")))
            )
        if nxt is None:
            return
        nxt = _aware(nxt)
        if until is not None and nxt >= until:
            return
        clock.t = max(clock.t, nxt)


def replay(
    source: Callable[[], Session],
    root: Path,
    store: BlobStore,
    workdir: Path,
    *,
    upto: str | None = None,
    app: AppDefinition = APP,
    extractor: Extractor | None = None,
    extractor_name: str = "",
) -> Replay:
    """root/runs의 실행을 처음부터(upto까지) workdir의 빈 DB에 다시 흘리고 실행마다 출력 해시를 낸다."""
    names = sorted(p.stem for p in (root / "runs").glob("*.json"))
    if upto is not None:
        names = [n for n in names if n <= upto]
    with source() as s:
        parses, decisions = recorded_parses(s), recorded_decisions(s)
        # 기록기 전의 DB에는 trace 표가 없다. 그때는 비교할 기록이 없을 뿐이다
        has_traces = sa.inspect(s.connection()).has_table(TraceRow.__tablename__)
        recorded = (
            {
                r.name: r
                for r in s.scalars(sa.select(TraceRow).where(TraceRow.kind == flight.COLLECT_RUN))
            }
            if has_traces
            else {}
        )
        s.expunge_all()

    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "replay.db"
    db.unlink(missing_ok=True)
    engine = make_engine(f"sqlite:///{db}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    runs_dir = workdir / "snapshots" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for old in runs_dir.glob("*.json"):
        old.unlink()
    clock = Clock()
    ctx = Context(
        root=runs_dir.parent,
        store=ReadOnlyStore(store),
        parser=RecordedParser(parses, store),
        extractor=extractor,
        extractor_name=extractor_name,
        now=clock,
        app=app,
    )
    waiting = list(decisions)
    bodies = {n: (root / "runs" / f"{n}.json").read_bytes() for n in names}
    times = [run_time(n, json.loads(bodies[n])) for n in names]
    for i, name in enumerate(names):
        (runs_dir / f"{name}.json").write_bytes(bodies[name])
        clock.t = max(clock.t, times[i])
        with factory() as s:
            for d in [d for d in waiting if d.at <= times[i]]:
                try:
                    drift.decide(s, d.source, d.key, d.status)
                    waiting.remove(d)
                except LookupError:
                    pass  # 아직 붙잡히지 않았다. 붙잡힌 뒤의 실행에서 다시 본다
            enqueue_load(s, runs_dir.parent, clock())
            s.commit()
        _drain(factory, ctx, clock, times[i + 1] if i + 1 < len(names) else None)

    with factory() as s:
        flight.close_finished(s, clock.t)
        s.commit()
        config = _without_parser(
            {
                "app": f"app:{app.id}@{app.version}",
                "matcher": None,
                "extractor": extractor_name or None,
            }
        )
        results = []
        for name in names:
            env = s.scalar(
                sa.select(EventRow).where(
                    EventRow.type == "source.run_finished",
                    EventRow.idempotency_key == name,
                )
            )
            trace = s.get(TraceRow, env.trace_id) if env is not None else None
            if trace is not None:
                config = _without_parser(trace.config)
            results.append(
                RunResult(
                    run=name,
                    trace_id=trace.id if trace else None,
                    digest=trace.digest if trace else None,
                    lines=flight.canonical_lines(s, trace.id) if trace else [],
                    recorded=recorded.get(name),
                    config=config,
                )
            )
        missing = [
            j.last_error or ""
            for j in s.scalars(sa.select(Job).where(Job.status == "dead"))
            if (j.last_error or "").startswith("MissingRecording")
        ]
        jobs = dict(s.execute(sa.select(Job.status, sa.func.count()).group_by(Job.status)).all())
    engine.dispose()
    with source() as s:
        for res in results:
            if res.recorded is not None:
                res.recorded_lines = flight.canonical_lines(s, res.recorded.id)
    return Replay(results, missing, jobs, db)


def compare(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
    """정규형 줄의 차이: (a에만, b에만). 같은 줄이 여러 번이면 개수까지 본다."""
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    return sorted((ca - cb).elements()), sorted((cb - ca).elements())


# --- 플랫폼 회귀 시험 (app-spec 15장): 재생 결과를 기준선으로 남기고, 코드를 고친 뒤 다시 재생해 비교 ---


def save_baseline(result: Replay, path: Path) -> None:
    """실행마다 정규형 줄과 해시를 남긴다(gzip JSON). 원문은 들어가지 않고 키와 개수만 들어간다."""
    import gzip

    body = {
        "platform_rev": flight.platform_rev(),
        "runs": {r.run: {"digest": r.digest, "lines": r.lines} for r in result.runs},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)


def load_baseline(path: Path) -> dict:
    import gzip

    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Regression:
    run: str
    only_base: list[str]  # 기준선에만 있던 봉투(정규형 줄)
    only_now: list[str]  # 지금 재생에만 있는 봉투


def against_baseline(result: Replay, baseline: dict) -> tuple[list[Regression], list[str]]:
    """(출력이 달라진 실행들, 기준선에 없던 실행들). 기준선에만 있는 실행은 재생 범위 밖이라 보지 않는다."""
    changed, new = [], []
    for r in result.runs:
        base = baseline["runs"].get(r.run)
        if base is None:
            new.append(r.run)
        elif base["digest"] != r.digest:
            only_base, only_now = compare(base["lines"], r.lines)
            changed.append(Regression(r.run, only_base, only_now))
    return changed, new
