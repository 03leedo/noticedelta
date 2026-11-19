"""에이전트 사용 기록과 개선 고리 (platform-services.md 10장).

도구 호출과 에이전트의 피드백을 남기고, 주간 보고로 묶어 도구를 고치는 데 쓴다.

- 값은 남기지 않는다. 인자는 모양(채운 칸·타입·길이)과 해시, 결과는 크기와 개수만 남긴다.
- 도구가 허락한 인자(`Tool.logged_args`)만 개인정보를 가리고 잘라 남긴다(예: 검색어).
- 피드백 글은 신뢰할 수 없는 입력이다. 개인정보를 가려 저장하고, 보고서에 보일 때는 지시로
  보이는 줄을 가린다. 자동으로 적용하지 않는다.
- 기록이 실패해도 도구 호출은 그대로 돈다(표준 오류로만 경고한다).
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import AgentFeedback, ToolCallRow
from noticedelta.llm.harness import mask, scan
from noticedelta.tooling import Tool, guard

log = logging.getLogger(__name__)

LOGGED_CHARS = 100  # 허락한 인자를 남기는 길이
FEEDBACK_CHARS = 1000
KINDS = {
    "wrong": "결과가 틀렸다",
    "lacking": "결과가 부족했다",
    "confusing": "쓰는 법이 헷갈렸다",
    "slow": "느렸다",
    "missing_tool": "필요한 도구가 없다",
    "other": "그 밖",
}


def _now() -> datetime:
    return datetime.now(UTC)


def shape(arguments: dict[str, Any]) -> dict[str, str]:
    """인자의 모양: 채운 칸마다 타입과 길이. 값은 넣지 않는다."""
    out = {}
    for key, value in sorted(arguments.items()):
        if value is None or value == "" or value == [] or value == {}:
            continue  # 비운 칸
        kind = type(value).__name__
        out[key] = f"{kind}:{len(value)}" if isinstance(value, str | list | dict) else kind
    return out


def digest(arguments: dict[str, Any]) -> str:
    """같은 호출의 반복을 찾는 해시. 밖으로 내보내지 않는다."""
    text = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean(text: str, limit: int) -> str:
    masked, _ = mask(text[:limit])
    return masked


def count(tool: Tool, result: dict | None) -> int | None:
    if result is None or tool.count is None:
        return None
    value = result.get(tool.count)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, list) else None


class UsageLog:
    """등록부의 관찰자(`tooling.Observer`). 접속 하나(세션)마다 하나 만든다."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        session: str | None = None,
        client: str = "mcp-stdio",
        now: Callable[[], datetime] = _now,
    ):
        self.factory = session_factory
        self.session = session or uuid.uuid4().hex
        self.client = client
        self.now = now
        self.seq = 0

    def _save(self, row) -> int | None:
        try:
            with self.factory() as s:
                s.add(row)
                s.commit()
                return row.id
        except Exception:  # 기록이 실패해도 도구 호출은 그대로 돈다
            log.warning("사용 기록을 남기지 못했다", exc_info=True)
            return None

    def record(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        result: dict | None,
        error: BaseException | None,
        duration_ms: int,
        hidden_lines: int,
    ) -> None:
        self.seq += 1
        if error is not None:
            status, message = "exception", type(error).__name__
        elif result is not None and result.get("error"):
            status, message = "error", str(result["error"])
        else:
            status, message = "ok", None
        logged = {
            k: _clean(str(arguments[k]), LOGGED_CHARS)
            for k in tool.logged_args
            if arguments.get(k) not in (None, "")
        }
        self._save(
            ToolCallRow(
                at=self.now(),
                session=self.session,
                client=self.client,
                seq=self.seq,
                tool=tool.name,
                tool_version=tool.version,
                arg_shape=shape(arguments),
                arg_digest=digest(arguments),
                logged=logged or None,
                status=status,
                error=_clean(message, 200) if message else None,
                result_bytes=len(json.dumps(result, ensure_ascii=False, default=str))
                if result is not None
                else 0,
                result_count=count(tool, result),
                duration_ms=duration_ms,
                hidden_lines=hidden_lines,
            )
        )

    def feedback(self, kind: str, message: str, tool: str = "") -> dict:
        kind = kind if kind in KINDS else "other"
        text = _clean(message, FEEDBACK_CHARS)
        flagged = sum(1 for line in text.split("\n") if scan(line))
        row_id = self._save(
            AgentFeedback(
                at=self.now(),
                session=self.session,
                client=self.client,
                tool=tool[:64] or None,
                kind=kind,
                message=text,
                flagged_lines=flagged,
                status="new",
            )
        )
        return {"received": row_id is not None, "id": row_id, "kind": kind}


def feedback_tool(usage: UsageLog) -> Tool:
    def send_feedback(kind: str, message: str, tool: str = "") -> dict:
        """도구가 기대와 다르게 동작하거나, 결과가 틀렸거나 부족하거나, 필요한 도구가 없을 때 남긴다.

        kind: wrong(틀림) / lacking(부족함) / confusing(헷갈림) / slow(느림) / missing_tool(없는 도구) / other.
        tool: 관련된 도구 이름(있으면). 개인정보나 문서 원문은 넣지 않는다.
        """
        return usage.feedback(kind, message, tool)

    return Tool(
        "send_feedback",
        "이 서버의 도구가 기대와 다르게 동작하거나, 결과가 틀렸거나 부족하거나, 필요한 도구가 없을 때 "
        "남긴다. 운영자가 읽고 도구를 고친다. 개인정보나 문서 원문은 넣지 않는다.",
        send_feedback,
    )


# --- 주간 보고 ---


def _pct(k: int, n: int) -> str:
    return f"{k / n:.0%}" if n else "-"


def report(
    session: Session,
    *,
    since: datetime,
    known_tools: Iterable[str] = (),
    top: int = 10,
) -> str:
    """사용 기록을 마크다운 보고서로. 결과 없는 검색어와 피드백은 가린 글만 보인다."""
    calls = session.scalars(
        sa.select(ToolCallRow)
        .where(ToolCallRow.at >= since)
        .order_by(ToolCallRow.session, ToolCallRow.seq)
    ).all()
    lines = [
        f"# 에이전트 사용 보고 ({since:%Y-%m-%d} 이후)",
        "",
        f"호출 {len(calls)}번, 세션 {len({c.session for c in calls})}개",
        "",
    ]

    by_tool: dict[str, list[ToolCallRow]] = defaultdict(list)
    for c in calls:
        by_tool[c.tool].append(c)
    retries: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    previous: ToolCallRow | None = None
    for c in calls:
        if previous is not None and previous.session == c.session:
            pairs[(previous.tool, c.tool)] += 1
            if previous.tool == c.tool and previous.status != "ok":
                retries[c.tool] += 1  # 오류 뒤 같은 도구를 다시 불렀다
        previous = c

    lines += [
        "## 도구별",
        "",
        "| 도구 | 판 | 호출 | 오류 | 오류 뒤 재시도 | 결과 없음 | 걸린 시간 중앙값 | 결과 크기 중앙값 | 가린 줄 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
        errors = sum(r.status != "ok" for r in rows)
        empty = sum(r.result_count == 0 for r in rows)
        versions = ",".join(sorted({r.tool_version for r in rows}))
        lines.append(
            f"| {name} | {versions} | {len(rows)} | {errors} ({_pct(errors, len(rows))}) "
            f"| {retries[name]} | {empty} | {statistics.median(r.duration_ms for r in rows):.0f}ms "
            f"| {statistics.median(r.result_bytes for r in rows):,.0f}B | {sum(r.hidden_lines for r in rows)} |"
        )
    unused = sorted(set(known_tools) - set(by_tool))
    lines += ["", f"안 쓰인 도구: {', '.join(unused) if unused else '없음'}", ""]

    errors = Counter((c.tool, c.error) for c in calls if c.status != "ok")
    if errors:
        lines += ["## 자주 난 오류", ""]
        lines += [f"- {tool}: {msg} ({n}번)" for (tool, msg), n in errors.most_common(top)]
        lines.append("")

    if pairs:
        lines += ["## 이어서 부른 도구", ""]
        lines += [f"- {a} → {b}: {n}번" for (a, b), n in pairs.most_common(top)]
        lines.append("")

    empty_inputs = Counter(
        (c.tool, json.dumps(c.logged, ensure_ascii=False, sort_keys=True))
        for c in calls
        if c.result_count == 0 and c.logged
    )
    if empty_inputs:
        lines += ["## 결과가 없던 입력 (새 출처·도구 후보)", ""]
        for (tool, logged), n in empty_inputs.most_common(top):
            shown, _ = guard(logged)
            lines.append(f"- {tool} {shown} ({n}번)")
        lines.append("")

    feedback = session.scalars(
        sa.select(AgentFeedback).where(AgentFeedback.at >= since).order_by(AgentFeedback.at)
    ).all()
    lines += [
        f"## 피드백 ({len(feedback)}건, 새 것 {sum(f.status == 'new' for f in feedback)}건)",
        "",
    ]
    for f in feedback:
        shown, hidden = guard(f.message)
        flag = f" · 지시로 보이는 줄 {hidden}개를 가림" if hidden else ""
        lines.append(f"- [{f.status}] #{f.id} {KINDS.get(f.kind, f.kind)} · {f.tool or '-'}{flag}")
        lines += [f"  > {line}" for line in shown.split("\n")]
    return "\n".join(lines)


def purge(session: Session, *, older_than_days: int, now: datetime | None = None) -> int:
    """보관 기한이 지난 사용 기록과 피드백을 지운다. 지운 행 수."""
    cutoff = (now or _now()) - timedelta(days=older_than_days)
    n = session.execute(sa.delete(ToolCallRow).where(ToolCallRow.at < cutoff)).rowcount or 0
    n += session.execute(sa.delete(AgentFeedback).where(AgentFeedback.at < cutoff)).rowcount or 0
    return n
