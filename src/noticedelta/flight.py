"""비행 기록기 (platform-services 3장): 묶음(trace)과 기록(record), 출력의 정규형과 해시.

결정적이지 않은 것은 경계를 지날 때 기록한다(3.2). 봉투는 베끼지 않고 trace_id로 엮기만 한다.

- 묶음: 수집 실행 하나가 collect_run 묶음 하나다. 플랫폼 판, 앱, 설정의 해시를 단다.
- 기록: 파서 호출(parse)과 LLM 호출(llm_call). 내용은 원문 저장소·녹화본에 있고, 기록에는 키와
  출력의 해시만 남긴다(녹화본 색인). 작업을 처리하는 동안 `bound`로 묶음을 걸어 두면 감싼 파서와
  게이트웨이의 관찰자가 거기에 쓴다.
- 정규형: 묶음의 봉투를 ID 대신 자연 키(출처/게시물 번호/판)로 바꾼 줄들. DB를 새로 만들어 ID가
  모두 달라도 같은 입력·같은 코드면 같은 줄이 나온다. 해시(digest)는 이 줄들의 해시다.
  기록 시각(recorded_at, occurred_at)은 넣지 않는다. 시계는 재생에서 주입하는 경계다.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.blobstore import blob_key
from noticedelta.db.models import (
    EventRow,
    Item,
    ItemVersion,
    Job,
    Notice,
    NoticeVersion,
    RecordRow,
    TraceRow,
)
from noticedelta.parsing import ParsedDocument, Parser

COLLECT_RUN = "collect_run"
OPEN, CLOSED = "open", "closed"
PENDING = ("queued", "running", "retry")  # 아직 끝나지 않은 작업


def sha(value: Any) -> str:
    # ensure_ascii: 깨진 HWP에서 나온 짝 없는 서로게이트도 인코딩 오류 없이 해시한다(재생이 찾은 버그)
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode("ascii")
    )
    return hashlib.sha256(raw).hexdigest()


@cache
def platform_rev() -> str:
    """지금 돌고 있는 플랫폼 코드의 판. NOTICEDELTA_REV가 있으면 그것, 없으면 git 커밋(고친 것이 있으면 +dirty)."""
    if rev := os.environ.get("NOTICEDELTA_REV"):
        return rev
    here = Path(__file__).resolve().parent
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=here,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "."],
            cwd=here,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if head.returncode != 0:
        return "unknown"
    return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")


# --- 묶음과 기록 ---


def open_trace(
    session: Session,
    trace_id: str,
    *,
    kind: str,
    started_at: datetime,
    config: dict[str, Any],
    name: str | None = None,
    app: str | None = None,
    parent_id: str | None = None,
) -> TraceRow:
    """묶음을 연다. 이미 있으면 그대로 준다(같은 실행을 다시 넣어도 한 번)."""
    row = session.get(TraceRow, trace_id)
    if row is None:
        row = TraceRow(
            id=trace_id,
            kind=kind,
            name=name,
            parent_id=parent_id,
            started_at=started_at,
            platform_rev=platform_rev(),
            app=app,
            config_hash=sha(config),
            config=config,
            status=OPEN,
        )
        session.add(row)
        session.flush()
    return row


def add_record(
    session: Session,
    trace_id: str,
    kind: str,
    *,
    ref: str,
    at: datetime,
    digest: str | None = None,
    cost: int | None = None,
    payload: dict | None = None,
) -> RecordRow:
    seq = session.scalar(
        sa.select(sa.func.max(RecordRow.seq)).where(RecordRow.trace_id == trace_id)
    )
    row = RecordRow(
        trace_id=trace_id,
        seq=(seq or 0) + 1,
        kind=kind,
        at=at,
        ref=ref[:200],
        digest=digest,
        cost=cost,
        payload=payload or {},
    )
    session.add(row)
    session.flush()
    return row


# 지금 처리 중인 작업의 (세션, 묶음, 시계). 감싼 파서·게이트웨이 관찰자가 여기에 쓴다.
_bound: contextvars.ContextVar[tuple[Session, str, Any] | None] = contextvars.ContextVar(
    "flight_bound", default=None
)


@contextmanager
def bound(session: Session, trace_id: str | None, now) -> Iterator[None]:
    token = _bound.set((session, trace_id, now) if trace_id else None)
    try:
        yield
    finally:
        _bound.reset(token)


def record_here(
    kind: str,
    *,
    ref: str,
    digest: str | None = None,
    cost: int | None = None,
    payload: dict | None = None,
) -> bool:
    """걸린 묶음이 있으면 기록을 남긴다. 없으면(묶음 밖의 호출) 아무것도 하지 않는다."""
    b = _bound.get()
    if b is None:
        return False
    session, trace_id, now = b
    add_record(
        session, trace_id, kind, ref=ref, at=now(), digest=digest, cost=cost, payload=payload
    )
    return True


class RecordingParser:
    """파서를 감싸 실제로 파싱한 호출을 기록한다(캐시에서 꺼낸 것은 파서를 부르지 않으므로 남지 않는다)."""

    def __init__(self, inner: Parser):
        self.inner = inner

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        doc = self.inner.parse(data, mime)
        record_here(
            "parse",
            ref=f"blob:{blob_key(data)}",
            digest=sha(doc.to_json()),
            payload={
                "mime": mime,
                "status": doc.status,
                "parser": f"{doc.parser} {doc.parser_version}".strip(),
            },
        )
        return doc


def llm_observer(key: str, req, resp) -> None:
    """게이트웨이 관찰자: 녹화본 키와 출력의 해시, 토큰을 남긴다(요청·응답 내용은 녹화본에 있다)."""
    record_here(
        "llm_call",
        ref=f"cassette:{key}",
        digest=sha(resp.output),
        cost=resp.input_tokens + resp.output_tokens,
        payload={
            "model": req.model,
            "template": req.template_version,
            "from_cassette": resp.from_cassette,
        },
    )


# --- 정규형 ---

_HEX32 = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
_ULID = re.compile(r"(?<![0-9A-Z])[0-9A-HJKMNP-TV-Z]{26}(?![0-9A-Z])")


class Namer:
    """DB의 ID를 자연 키로 바꾼다. 모르는 ID는 "<id>"(비교에서 빠진다)."""

    def __init__(self, session: Session):
        self.s = session
        self.names: dict[str, str] = {}

    def _notice(self, nv: NoticeVersion) -> str:
        n = self.s.get(Notice, nv.notice_id)
        return f"{n.source_id}/{n.external_id}"

    def name(self, id_: str) -> str:
        if id_ in self.names:
            return self.names[id_]
        self.names[id_] = "<id>"  # 서로를 가리켜도 끝나게
        out = "<id>"
        if _ULID.fullmatch(id_):
            env = self.s.get(EventRow, id_)
            if env is not None:
                out = f"ev:{env.type}/{self.canon(env.idempotency_key)}"
        elif (pv := self.s.get(ItemVersion, id_)) is not None:
            out = f"{self.name(pv.item_id)}@v{pv.version_no}"
        elif (nv := self.s.get(NoticeVersion, id_)) is not None:
            out = f"nv:{self._notice(nv)}@{nv.version_no}"
        elif self.s.get(Item, id_) is not None:
            first = self.s.scalar(
                sa.select(NoticeVersion)
                .join(ItemVersion, ItemVersion.notice_version_id == NoticeVersion.id)
                .where(ItemVersion.item_id == id_)
                .order_by(ItemVersion.version_no)
                .limit(1)
            )
            out = f"item:{self._notice(first)}" if first is not None else "<id>"
        elif (n := self.s.get(Notice, id_)) is not None:
            out = f"notice:{n.source_id}/{n.external_id}"
        self.names[id_] = out
        return out

    def canon(self, value: Any) -> Any:
        if isinstance(value, str):
            value = _ULID.sub(lambda m: self.name(m.group()), value)
            return _HEX32.sub(lambda m: self.name(m.group()), value)
        if isinstance(value, list):
            return [self.canon(v) for v in value]
        if isinstance(value, dict):
            return {k: self.canon(v) for k, v in value.items()}
        return value


def canonical_lines(session: Session, trace_id: str) -> list[str]:
    """묶음의 봉투를 정규형 줄로. 작업을 꺼낸 순서와 무관하게 비교하려고 정렬한다."""
    namer = Namer(session)
    lines = []
    for env in session.scalars(sa.select(EventRow).where(EventRow.trace_id == trace_id)):
        lines.append(
            json.dumps(
                {
                    "type": env.type,
                    "v": env.v,
                    "producer": env.producer,
                    "key": namer.canon(env.idempotency_key),
                    "app": env.app,
                    "subject": namer.canon(env.subject),
                    "item": namer.canon(env.item),
                    "item_version": env.item_version,
                    "cause": namer.canon(env.causation_id),
                    "labels": namer.canon(env.labels),
                    "payload": namer.canon(env.payload),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return sorted(lines)


def digest_of(lines: list[str]) -> str:
    return sha("\n".join(lines).encode("utf-8", "surrogatepass"))


def pending_traces(session: Session) -> set[str]:
    """끝나지 않은 작업이 아직 실어 나르는 묶음."""
    payloads = session.scalars(sa.select(Job.payload).where(Job.status.in_(PENDING)))
    return {p["trace"] for p in payloads if isinstance(p, dict) and p.get("trace")}


def close_finished(session: Session, now: datetime) -> list[TraceRow]:
    """남은 작업이 없는 열린 묶음을 닫고 출력의 정규형 해시를 적는다. 워커가 일을 마칠 때 부른다."""
    busy = pending_traces(session)
    closed = []
    for row in session.scalars(sa.select(TraceRow).where(TraceRow.status == OPEN)):
        if row.id in busy:
            continue
        lines = canonical_lines(session, row.id)
        row.digest, row.events = digest_of(lines), len(lines)
        row.status, row.ended_at = CLOSED, now
        closed.append(row)
    session.flush()
    return closed


def spent_tokens(session: Session, since: datetime) -> int:
    """since 이후 실제로 보낸 LLM 호출의 토큰(입력+출력). 녹화본에서 꺼낸 호출은 세지 않는다.

    워커의 하루 한도가 이것으로 남은 양을 정한다. 기록은 묶음이 걸린 작업에서만 남으므로, 추출 작업은
    늘 묶음을 싣고 다닌다(수집 실행의 묶음, 운영자가 넣은 작업은 그 일의 묶음).
    """
    rows = session.execute(
        sa.select(RecordRow.cost, RecordRow.payload).where(
            RecordRow.kind == "llm_call", RecordRow.at >= since
        )
    )
    return sum(cost or 0 for cost, payload in rows if not (payload or {}).get("from_cassette"))
