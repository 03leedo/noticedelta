"""도구 게이트웨이: 에이전트의 모든 도구 호출이 지나는 유일한 길 (platform-services 5장).

    gate = ToolGateway(tools, chosen=["read_notice", "get_profile", "send_inquiry"], cipher=cipher)
    s = gate.open_session(db, app_id="grant-alerts", agent="claude-code", person_id=pid, now=now)
    out = gate.call(db, s.id, "send_inquiry", {"to": "...", "body": "..."}, now=now)
    gate.approve(db, action_id, by="person:...", now=now)   # 승인함에서
    gate.commit_due(db, now=now)                           # 보류 시간이 지난 효과를 실행한다

호출마다 차례로 본다(5.4).

1. 선언: 등록된 도구인가, 앱이 쓰기로 고른 도구인가, 한도 안인가. 아니면 막는다.
2. 동의와 허가: B 도구면 금고에서 이 세션 이름(session:<id>)으로 한 번 쓰는 허가를 받아 값을 꺼내
   도구의 profile 인자로 넣는다. B 도구는 금고를 직접 열지 않는다. 동의가 없으면 막는다.
3. 전이: 밖으로 나가는 효과(external, C)로 세션이 A·B·C를 모두 갖게 되면 정책을 적용한다.
   서식(form)이 있는 도구는 split(서식 칸만 남겨 다시 만든다), 없으면 approve(사람의 승인).
   C는 효과마다 새로 켜지는 것으로 본다. 한 번 승인한 세션의 다음 효과도 다시 판단한다.
   그 밖의 호출은 그대로 통과한다(external은 보류 시간 뒤에 실행).
4. 받는 쪽: 허용 목록 밖의 받는 쪽은 표시한다. 그 값을 이 세션이 외부 글(A)에서 봤으면 막고, 아니면
   사람의 승인을 받게 한다. 승인해도 표시는 남는다.
5. 기록: 판단과 이유를 tool_decision에 남긴다(인자 값 없이). 비행 기록기의 묶음이 걸려 있으면 거기도.

효과(5.5): external은 바로 실행하지 않고 tool_action으로 둔다.

    held(승인 기다림) → approved(보류 시간 기다림, 이때도 취소할 수 있다) → committing → committed
    또는 discarded(거절·취소·세션 닫음) · expired(승인 기한 지남) · blocked · failed

internal(플랫폼 안의 되돌릴 수 있는 상태)은 바로 반영하고, 도구가 결과에 넣은 undo 정보를 남겨
되돌릴 수 있게 한다. 상태가 바뀔 때마다 action.* 봉투를 낸다.

비밀값(5.6): C 도구의 계정·토큰은 운영자가 도구를 만들 때 넣는다. 에이전트의 인자는 도구 서명에 있는
칸만 받으므로 자격증명을 끼워 넣을 수 없다.

한계(5.9): 세션 단위 표시는 거칠다. 외부 글을 한 번 본 세션은 닫을 때까지 A다. 받는 쪽의 출처는
값의 해시로만 따진다(외부 글에서 본 주소인가). 인자 하나하나의 출처는 따지지 않는다.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import events, flight, vault
from noticedelta.db.models import AgentSessionRow, ToolActionRow, ToolDecisionRow
from noticedelta.tooling import Observer, Tool, guard

PRODUCER = "tool_gateway"
HOLD = timedelta(minutes=10)  # external 효과의 기본 보류 시간
DECIDE_WITHIN = timedelta(hours=24)  # 승인 기한. 지나면 expired
MAX_EFFECTS = 5  # 세션 하나가 요청할 수 있는 external 효과 수
INJECTED = "profile"  # 게이트웨이가 B 도구에 넣는 인자(금고에서 꺼낸 값)
ACTOR = "actor"  # 게이트웨이가 넣는 인자: 부른 사람("person:<id>" 또는 None)
GATE_ARGS = frozenset({INJECTED, ACTOR})  # 에이전트가 줄 수 없는 인자
MAX_SEEN = 5000
MAX_SOURCES = 50
ALL = frozenset("ABC")

# 받는 쪽으로 쓰일 수 있는 값: 메일 주소, 웹 주소, 전화번호. ASCII만 잡는다(바로 붙은 한글 조사를
# 주소에 넣지 않게: "help@evil.example로")
_REACHABLE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"|https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+"
    r"|(?:\+82[-. ]?|0)\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}"
)


class Blocked(Exception):
    """게이트웨이가 막은 호출(사용 기록에 남길 때 쓴다)."""


class SessionClosed(KeyError):
    """없거나 닫힌 세션으로 불렀다."""


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=UTC) if t is not None and t.tzinfo is None else t


def normalize(value: Any) -> str:
    v = str(value).strip().lower().rstrip(".,;:/")
    if re.fullmatch(r"\+?[\d\-. ()]+", v):  # 전화번호는 숫자만, 국가번호(+82)는 0으로
        digits = re.sub(r"\D", "", v)
        v = "0" + digits[2:] if v.startswith("+82") else digits
    return v


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def reachable(value: Any) -> set[str]:
    """결과 안의 글에서 받는 쪽으로 쓰일 수 있는 값(정규화한 것)."""
    out: set[str] = set()
    if isinstance(value, str):
        out |= {normalize(m) for m in _REACHABLE.findall(value)}
    elif isinstance(value, list | tuple):
        for v in value:
            out |= reachable(v)
    elif isinstance(value, dict):
        for v in value.values():
            out |= reachable(v)
    return out


def _flags(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


class ToolGateway:
    def __init__(
        self,
        tools: Iterable[Tool],
        *,
        chosen: Collection[str],
        cipher: vault.Cipher | None = None,
        observer: Observer | None = None,
        hold: timedelta = HOLD,
        decide_within: timedelta = DECIDE_WITHIN,
        max_effects: int = MAX_EFFECTS,
        chosen_by_app: Callable[[Session, str], Collection[str] | None] | None = None,
    ):
        """chosen_by_app: (db, 세션의 앱) → 그 앱이 고른 도구(앱 정의의 agent.tools). None이면 모두."""
        self.tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError(f"같은 이름의 도구가 이미 있다: {tool.name}")
            params = inspect.signature(tool.handler).parameters
            if ("B" in tool.capabilities) != (INJECTED in params):
                raise ValueError(
                    f"{tool.name}: 금고 값은 B 도구만 {INJECTED} 인자로 받는다(게이트웨이가 넣는다)"
                )
            required = {
                n
                for n, prm in params.items()
                if prm.default is inspect.Parameter.empty
                and prm.kind not in (prm.VAR_POSITIONAL, prm.VAR_KEYWORD)
            }
            if tool.form and not required <= set(tool.form):
                missing = sorted(required - set(tool.form))
                raise ValueError(f"{tool.name}: 서식(form)에 꼭 받아야 하는 인자가 없다: {missing}")
            self.tools[tool.name] = tool
        unknown = set(chosen) - set(self.tools)
        if unknown:
            raise ValueError(f"앱이 고른 도구가 등록되지 않았다: {sorted(unknown)}")
        self.chosen = frozenset(chosen)
        self.cipher = cipher
        self.observer = observer
        self.hold = hold
        self.decide_within = decide_within
        self.max_effects = max_effects
        self.chosen_by_app = chosen_by_app

    # --- 세션 (5.3) ---

    def open_session(
        self,
        db: Session,
        *,
        app_id: str,
        agent: str,
        person_id: str | None = None,
        now: datetime,
    ) -> AgentSessionRow:
        """(사람, 앱, 에이전트)의 열린 세션을 잇는다. 없으면 연다. 켜진 속성은 다시 접속해도 그대로다."""
        agent = agent[
            :64
        ]  # 저장하는 길이로 찾는다(긴 이름으로 새 세션을 열어 속성을 비우지 못하게)
        who = (
            AgentSessionRow.person_id.is_(None)
            if person_id is None
            else AgentSessionRow.person_id == person_id
        )
        row = db.scalar(
            sa.select(AgentSessionRow)
            .where(
                who,
                AgentSessionRow.app_id == app_id,
                AgentSessionRow.agent == agent,
                AgentSessionRow.status == "open",
            )
            .order_by(AgentSessionRow.started_at.desc())
            .limit(1)
        )
        if row is None:
            row = AgentSessionRow(
                id=events.new_ulid(now),
                person_id=person_id,
                app_id=app_id,
                agent=agent,
                flags=[],
                seen=[],
                sources=[],
                calls={},
                effects=0,
                started_at=now,
                last_seen_at=now,
                status="open",
            )
            db.add(row)
            db.flush()
        return row

    def close_session(
        self, db: Session, session_id: str, *, now: datetime, by: str = "operator"
    ) -> None:
        """세션을 닫는다(사람·운영자가). 아직 나가지 않은 효과는 버린다. 다음 접속은 속성이 빈 새 세션이다."""
        row = db.get(AgentSessionRow, session_id)
        if row is None or row.status != "open":
            return
        row.status, row.closed_at = "closed", now
        pending = db.scalars(
            sa.select(ToolActionRow).where(
                ToolActionRow.session_id == session_id,
                ToolActionRow.status.in_(("held", "approved")),
            )
        ).all()
        for a in pending:
            if self._take(db, a.id, ("held", "approved"), "discarded"):
                a = self._action(db, a.id)
                a.decided_at, a.decided_by = now, by[:64]
                a.reason = f"{a.reason or ''} / 세션을 닫아 버렸다"[-300:]
                self._emit(db, a, row, "discarded", now)

    def forget_person(self, db: Session, person_id: str, *, now: datetime) -> int:
        """사람이 자기 정보를 지울 때: 그 사람의 세션을 닫고(나가지 않은 효과는 버린다), 효과에 남은
        인자·결과(본문에 넣은 내 정보 등)를 지운다. 지운 효과 수."""
        sessions = db.scalars(
            sa.select(AgentSessionRow).where(AgentSessionRow.person_id == person_id)
        ).all()
        for s in sessions:
            self.close_session(db, s.id, now=now, by="person:forget")
        ids = [s.id for s in sessions]
        if not ids:
            return 0
        took = db.execute(
            sa.update(ToolActionRow)
            .where(ToolActionRow.session_id.in_(ids))
            .values(arguments={}, result=None, undo=None)
            .execution_options(synchronize_session=False)
        )
        return took.rowcount

    # --- 호출 (5.4) ---

    def call(
        self,
        db: Session,
        session_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        now: datetime,
    ) -> dict:
        """도구 하나를 부른다. 막으면 {"blocked": …}, 밖으로 나가는 효과는 {"action": …}를 돌려준다.

        스스로 커밋한다: 도구를 실행하기 직전(여기까지의 판단과 금고 접근 기록)과 끝날 때. 사용 기록
        관찰자는 커밋한 뒤에 부른다(관찰자가 자기 DB 세션으로 써도 잠기지 않게). 세션 행은 잠가 읽는다.
        """
        s = db.get(AgentSessionRow, session_id, with_for_update=True)
        if s is None or s.status != "open":
            raise SessionClosed(f"열린 세션이 아니다: {session_id}")
        s.last_seen_at = now
        args = dict(arguments or {})
        before = set(s.flags)
        tool = self.tools.get(name)

        # 1. 선언
        why = self._undeclared(s, name, tool, args) or self._not_for_app(db, s, name)
        if why is not None:
            return self._refuse(db, s, name, tool, args, "declare", why, before, now)
        params = inspect.signature(tool.handler).parameters
        if ACTOR in params:  # 부른 사람은 게이트웨이가 넣는다(효과라면 인자와 함께 남는다)
            args[ACTOR] = f"person:{s.person_id}" if s.person_id else None
        probe = {**args, INJECTED: {}} if "B" in tool.capabilities else args
        try:
            inspect.signature(tool.handler).bind(**probe)
        except TypeError as e:
            reason = f"인자가 도구와 맞지 않는다: {e}"[:300]
        else:
            try:
                reason = tool.validate(dict(args)) if tool.validate is not None else None
            except Exception as e:  # 검사가 깨져도 이유로 남긴다
                reason = f"인자를 검사하지 못했다: {type(e).__name__}"
        if reason is not None:
            reason = reason[:300]
            self._decide(db, s, name, "error", "declare", reason, before, before, now)
            db.commit()
            return {"error": reason}
        if tool.authorize is not None:
            try:
                why = tool.authorize(db, s.person_id, dict(args))
            except Exception as e:
                why = f"권한을 확인하지 못했다: {type(e).__name__}"
            if why is not None:
                return self._refuse(db, s, name, tool, args, "authorize", why, before, now)
        s.calls = {**s.calls, name: s.calls.get(name, 0) + 1}
        if tool.effect == "external":
            return self._request(db, s, tool, args, before, now)

        # 2. 동의와 허가
        if "B" in tool.capabilities:
            values, why = self._read_vault(db, s, tool, now)
            if why is not None:
                return self._refuse(db, s, name, tool, args, "consent", why, before, now)
            args[INJECTED] = values

        # 실행(효과 없음·internal). 그 전에 여기까지의 판단(호출 수, 금고 허가·접근 기록)을 커밋한다:
        # 도구가 자기 DB 세션을 열어도 잠기지 않고(SQLite), 도구가 실패해도 금고 접근 기록은 남는다
        db.commit()
        started = time.perf_counter()
        try:
            raw = tool.handler(**args)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:300]
            self._decide(db, s, name, "error", "run", reason, before, before, now)
            db.commit()
            self._observe(tool, args, None, e, started, 0)
            raise
        undo = raw.pop("undo", None) if isinstance(raw, dict) else None
        result, hidden = guard(raw)
        if hidden:
            result["guard"] = {"hidden_lines": hidden, "why": "지시로 보이는 줄을 가렸습니다"}
        # 실행하는 동안 다른 접속이 같은 세션의 속성을 켰을 수 있다: 다시 읽어 합친다(켜지기만 한다)
        db.refresh(s, with_for_update=True)
        after = set(s.flags)
        if "A" in tool.capabilities:
            after.add("A")
            self._saw(s, tool, args, raw, now)
        if "B" in tool.capabilities:
            after.add("B")
        s.flags = _flags(after)
        action_id = None
        if tool.effect == "internal":
            action_id = self._internal(db, s, tool, args, undo, now)
            result["action"] = {"id": action_id, "status": "committed", "undo": undo is not None}
        self._decide(db, s, name, "pass", "run", None, before, after, now, action_id)
        db.commit()
        self._observe(tool, args, result, None, started, hidden)
        return result

    def _not_for_app(self, db: Session, s: AgentSessionRow, name: str) -> str | None:
        if self.chosen_by_app is None:
            return None
        picked = self.chosen_by_app(db, s.app_id)
        if picked is not None and name not in picked:
            return f"앱 {s.app_id}이 쓰기로 고르지 않은 도구(앱 정의의 agent.tools)"
        return None

    def _undeclared(
        self, s: AgentSessionRow, name: str, tool: Tool | None, args: dict
    ) -> str | None:
        if tool is None:
            return f"등록되지 않은 도구: {name}"
        if name not in self.chosen:
            return "앱이 쓰기로 고르지 않은 도구"
        if tool.max_calls is not None and s.calls.get(name, 0) >= tool.max_calls:
            return f"이 세션에서 부를 수 있는 수({tool.max_calls}회)를 넘었다"
        if tool.effect == "external" and s.effects >= self.max_effects:
            return f"이 세션의 외부 효과 한도({self.max_effects}건)를 넘었다"
        if GATE_ARGS & set(args):
            return f"{', '.join(sorted(GATE_ARGS & set(args)))}는 게이트웨이가 넣는 칸이다"
        return None

    def _read_vault(
        self, db: Session, s: AgentSessionRow, tool: Tool, now: datetime
    ) -> tuple[dict, str | None]:
        if s.person_id is None:
            return {}, "사람이 정해지지 않은 세션은 금고를 읽지 못한다"
        if self.cipher is None:
            return {}, "금고 열쇠가 없다"
        consent = vault.active_consent(db, s.person_id, s.app_id, tool.purpose)
        if consent is None:
            return {}, f"{tool.purpose} 용도의 동의가 없다"
        keys = sorted(set(tool.keys) & set(consent.keys))
        if not keys:
            return {}, "이 도구가 쓰는 키에 동의하지 않았다"
        try:
            grant = vault.issue(
                db, s.person_id, s.app_id, tool.purpose, keys, holder=f"session:{s.id}", now=now
            )
            return vault.read(db, self.cipher, grant.id, now=now), None
        except vault.VaultError as e:
            return {}, str(e)

    def _saw(self, s: AgentSessionRow, tool: Tool, args: dict, raw: Any, now: datetime) -> None:
        """외부 글(A)을 돌려준 호출: 그 안의 주소·전화(해시)와 무엇을 봤는지 남긴다."""
        hashes = sorted({_h(v) for v in reachable(raw)} - set(s.seen))
        if hashes:
            s.seen = [*s.seen, *hashes][-MAX_SEEN:]
        ref = {k: str(args[k])[:80] for k in tool.logged_args if k in args}
        entry = {"tool": tool.name, "args": ref, "at": now.isoformat()}
        s.sources = [*s.sources, entry][-MAX_SOURCES:]

    def _internal(
        self, db: Session, s: AgentSessionRow, tool: Tool, args: dict, undo: Any, now: datetime
    ) -> str:
        a = ToolActionRow(
            id=events.new_ulid(now),
            session_id=s.id,
            tool=tool.name,
            effect="internal",
            arguments={k: v for k, v in args.items() if k != INJECTED},
            dropped=[],
            status="committed",
            policy="pass",
            marks=[],
            reason="플랫폼 안의 되돌릴 수 있는 효과라 바로 반영",
            evidence=list(s.sources),
            requested_at=now,
            decided_at=now,
            decided_by="policy:internal",
            committed_at=now,
            undo=undo if isinstance(undo, dict) else None,
        )
        db.add(a)
        db.flush()
        self._emit(db, a, s, "committed", now)
        return a.id

    def _marks(self, db: Session, s: AgentSessionRow, tool: Tool, args: dict) -> list[dict]:
        """받는 쪽 검사(5.4의 4번). 허용 목록 밖이면 표시하고, 외부 글에서 본 값인지 적는다."""
        if not tool.targets:
            return []
        allowed = {normalize(v) for v in tool.allow(db, dict(args))}
        seen = set(s.seen)
        marks = []
        for arg in tool.targets:
            value = args.get(arg)
            values = value if isinstance(value, list | tuple) else [value]
            for one in values:
                if one is None or one == "":
                    continue
                n = normalize(one)
                if n in allowed:
                    continue
                marks.append(
                    {"arg": arg, "mark": "from_untrusted" if _h(n) in seen else "unlisted"}
                )
        return marks

    def _request(
        self, db: Session, s: AgentSessionRow, tool: Tool, args: dict, before: set, now: datetime
    ) -> dict:
        """밖으로 나가는 효과: 실행하지 않고 tool_action으로 둔다(5.5)."""
        s.effects += 1
        marks = self._marks(db, s, tool, args)
        transition = (before | tool.capabilities) >= ALL
        dropped: list[str] = []
        kinds = {m["mark"] for m in marks}
        if "from_untrusted" in kinds:
            policy, status = "block", "blocked"
            reason = "받는 쪽이 허용 목록에 없고, 이 세션이 외부 글(A)에서 본 값이다"
        elif transition and tool.form:
            policy = "split"
            dropped = sorted(set(args) - set(tool.form))
            args = {k: v for k, v in args.items() if k in tool.form}
            reason = "이 효과로 세션이 A·B·C를 모두 갖게 되어 서식 칸만으로 다시 만들었다"
            if dropped:
                reason += f"(뺀 칸: {', '.join(dropped)})"
            status = "held" if marks else "approved"
        elif transition:
            policy, status = "approve", "held"
            reason = "이 효과로 세션이 A·B·C를 모두 갖게 되어 사람의 승인을 받는다"
        else:
            policy, status = "pass", "held" if marks else "approved"
            have = "·".join(sorted(before)) or "없음"
            reason = f"세션의 속성이 {have}라 그대로 통과한다(보류 시간 뒤 실행)"
        if "unlisted" in kinds and status == "held":
            reason += ". 받는 쪽이 허용 목록에 없어 승인을 받는다"
        if tool.confirm and status == "approved":
            status = "held"
            reason += ". 사람의 확인이 꼭 필요한 일이라 승인을 받는다"
        hold = self._hold(tool)
        approved = status == "approved"
        a = ToolActionRow(
            id=events.new_ulid(now),
            session_id=s.id,
            tool=tool.name,
            effect="external",
            arguments=args,
            dropped=dropped,
            status=status,
            policy=policy,
            marks=marks,
            reason=reason[:300],
            evidence=list(s.sources),
            requested_at=now,
            decide_by=now + self.decide_within if status == "held" else None,
            decided_at=now if approved else None,
            decided_by=f"policy:{policy}" if approved else None,
            approved_at=now if approved else None,
            approved_by=f"policy:{policy}" if approved else None,
            commit_after=now + hold if approved else None,
        )
        db.add(a)
        db.flush()
        self._emit(db, a, s, "requested", now)
        self._emit(db, a, s, status, now)
        decision = {"blocked": "block", "held": "held"}.get(status, policy)
        step = "target" if marks else "transition"
        self._decide(db, s, tool.name, decision, step, a.reason, before, before, now, a.id)
        out = self._view(a)
        db.commit()
        self._observe(tool, args, out, Blocked(reason) if status == "blocked" else None, None, 0)
        return {"action": out}

    def _refuse(
        self,
        db: Session,
        s: AgentSessionRow,
        name: str,
        tool: Tool | None,
        args: dict,
        step: str,
        reason: str,
        before: set,
        now: datetime,
    ) -> dict:
        self._decide(db, s, name, "block", step, reason, before, before, now)
        db.commit()
        if tool is not None:
            self._observe(tool, args, None, Blocked(reason), None, 0)
        return {"blocked": {"step": step, "reason": reason}}

    # --- 보류 후 확정 (5.5) ---

    def approve(self, db: Session, action_id: str, *, by: str, now: datetime) -> ToolActionRow:
        """승인한다. 보류 시간이 지나야 실행되고, 그동안은 취소할 수 있다."""
        a = self._action(db, action_id)
        if a.status != "held":
            raise ValueError(f"승인할 수 없는 상태: {a.status}")
        if a.decide_by is not None and _aware(a.decide_by) <= now:
            if self._take(db, action_id, ("held",), "expired"):
                self._emit(db, self._action(db, action_id), None, "expired", now)
                db.commit()  # 기한이 지난 것은 닫아 둔다(승인함에 다시 보이지 않게)
            raise ValueError("승인 기한이 지났다")
        if any(m["mark"] == "from_untrusted" for m in a.marks):
            raise ValueError("외부 글에서 본 받는 쪽으로는 승인할 수 없다")
        if not self._take(db, action_id, ("held",), "approved"):
            raise ValueError("그 사이 상태가 바뀌었다")
        a = self._action(db, action_id)
        a.decided_at, a.decided_by = now, by[:64]
        a.approved_at, a.approved_by = now, by[:64]
        a.commit_after = now + self._hold(self.tools.get(a.tool))
        self._emit(db, a, db.get(AgentSessionRow, a.session_id), "approved", now)
        return a

    def discard(
        self, db: Session, action_id: str, *, by: str, now: datetime, why: str | None = None
    ) -> ToolActionRow:
        """거절하거나(held), 승인한 뒤 보류 시간 안에 취소한다(approved)."""
        # 조건부로 바꾼다: 워커가 그 사이 committing으로 차지했으면(이미 나가는 중) 취소는 실패다
        if not self._take(db, action_id, ("held", "approved"), "discarded"):
            status = self._action(db, action_id).status
            raise ValueError(f"거절·취소할 수 없는 상태: {status}")
        a = self._action(db, action_id)
        a.decided_at, a.decided_by = now, by[:64]
        if why:
            a.reason = f"{a.reason or ''} / {why}"[-300:]
        self._emit(db, a, db.get(AgentSessionRow, a.session_id), "discarded", now)
        return a

    def edit(
        self, db: Session, action_id: str, arguments: dict[str, Any], *, by: str, now: datetime
    ) -> ToolActionRow:
        """승인 전의 효과를 사람이 고친다. 받는 쪽은 다시 검사하고, 고친 뒤에도 승인을 받는다."""
        a = self._action(db, action_id)
        if a.status != "held":
            raise ValueError(f"고칠 수 없는 상태: {a.status}")
        tool = self.tools[a.tool]
        if GATE_ARGS & set(arguments):
            raise ValueError("게이트웨이가 넣은 칸은 고칠 수 없다")
        if a.policy == "split" and not set(arguments) <= set(tool.form):
            raise ValueError(f"서식으로 다시 만든 효과는 서식 칸만 고친다: {sorted(tool.form)}")
        merged = {**a.arguments, **arguments}
        inspect.signature(tool.handler).bind(**merged)  # 맞지 않으면 TypeError
        if tool.validate is not None and (why := tool.validate(dict(merged))):
            raise ValueError(why)
        s = db.get(AgentSessionRow, a.session_id)
        a.arguments = merged
        a.marks = self._marks(db, s, tool, merged)  # 사람이 고쳐도 표시는 남긴다
        a.reason = f"{a.reason or ''} / {by[:40]}가 고쳤다"[-300:]
        if any(m["mark"] == "from_untrusted" for m in a.marks):
            a.reason = f"{a.reason} / 받는 쪽이 외부 글에서 본 값이라 막았다"[-300:]
            a.decided_at, a.decided_by = now, by[:64]
            self._close(db, a, "blocked", now)
        return a

    def commit_due(self, db: Session, *, now: datetime) -> list[ToolActionRow]:
        """승인 기한이 지난 효과를 닫고, 보류 시간이 지난 승인된 효과를 실행한다.

        스스로 커밋한다. 실행하기 전에 효과를 committing으로 차지해 커밋하므로, 워커가 둘이거나
        실행 뒤 커밋이 실패해도 같은 효과가 두 번 나가지 않는다(많아야 한 번). 실행하다 멈춰
        committing에 남은 효과는 운영자가 본다. 다른 트랜잭션 안에서 부르지 않는다.
        """
        late = db.scalars(
            sa.select(ToolActionRow).where(
                ToolActionRow.status == "held", ToolActionRow.decide_by <= now
            )
        ).all()
        for a in late:
            self._close(db, a, "expired", now)
        db.commit()
        due = db.scalars(
            sa.select(ToolActionRow.id)
            .where(ToolActionRow.status == "approved", ToolActionRow.commit_after <= now)
            .order_by(ToolActionRow.commit_after)
        ).all()
        done = []
        for action_id in due:
            took = db.execute(
                sa.update(ToolActionRow)
                .where(ToolActionRow.id == action_id, ToolActionRow.status == "approved")
                .values(status="committing")
                .execution_options(synchronize_session=False)
            )
            db.commit()
            if took.rowcount != 1:
                continue  # 다른 워커가 먼저 차지했다
            a = self._action(db, action_id)
            s = db.get(AgentSessionRow, a.session_id)
            tool = self.tools.get(a.tool)
            if s is None or s.status != "open":
                a.reason = f"{a.reason or ''} / 세션이 닫혀 실행하지 않았다"[-300:]
                self._close(db, a, "discarded", now)
                db.commit()
                continue
            if tool is None:
                a.result = {"error": f"등록되지 않은 도구: {a.tool}"}
                self._close(db, a, "failed", now)
                db.commit()
                continue
            arguments = dict(a.arguments)
            db.commit()  # 실행하는 동안 트랜잭션을 쥐지 않는다(도구가 자기 DB 세션을 열 수 있게)
            try:
                result, error = tool.handler(**arguments), None
            except Exception as e:
                result, error = None, f"{type(e).__name__}: {e}"[:300]
            a = self._action(db, action_id)
            if error is not None:
                a.result = {"error": error}
                self._close(db, a, "failed", now)
            else:
                s = db.get(AgentSessionRow, a.session_id, with_for_update=True)
                a.result = result if isinstance(result, dict) else {"value": result}
                a.committed_at = now
                s.flags = _flags([*s.flags, "C"])  # C는 효과가 확정될 때 켜진다
                self._close(db, a, "committed", now)
                done.append(a)
            db.commit()
        return done

    def undo(self, db: Session, action_id: str, *, by: str, now: datetime) -> ToolActionRow:
        """플랫폼 안의 효과(internal)를 되돌린다. 스스로 커밋한다.

        undoing으로 차지해 커밋한 뒤 되돌린다(되돌리는 함수가 자기 DB 세션을 열 수 있게, 두 번 되돌리지
        않게). 되돌리다 실패하면 committed로 돌려 둔다.
        """
        a = self._action(db, action_id)
        if a.effect != "internal" or a.status != "committed" or a.undo is None:
            raise ValueError("되돌릴 수 있는 효과가 아니다")
        info, tool = dict(a.undo), self.tools[a.tool]
        if not self._take(db, action_id, ("committed",), "undoing"):
            raise ValueError("그 사이 상태가 바뀌었다")
        db.commit()
        try:
            tool.undo(**info)
        except Exception:
            self._take(db, action_id, ("undoing",), "committed")
            db.commit()
            raise
        a = self._action(db, action_id)
        a.undone_at = now
        a.reason = f"{a.reason or ''} / {by[:40]}가 되돌렸다"[-300:]
        self._close(db, a, "undone", now)
        db.commit()
        return a

    # --- 승인함과 요약 ---

    def inbox(self, db: Session, *, person_id: str | None = None) -> list[dict]:
        """사람에게 보일 승인 카드: 무엇을, 누구에게, 무엇을 근거로, 언제 나가는지(5.5)."""
        q = (
            sa.select(ToolActionRow, AgentSessionRow)
            .join(AgentSessionRow, AgentSessionRow.id == ToolActionRow.session_id)
            .where(ToolActionRow.status.in_(("held", "approved")))
            .order_by(ToolActionRow.requested_at)
        )
        if person_id is not None:
            q = q.where(AgentSessionRow.person_id == person_id)
        cards = []
        for a, s in db.execute(q).all():
            tool = self.tools.get(a.tool)
            cards.append(
                {
                    **self._view(a),
                    "what": tool.description if tool else a.tool,
                    "arguments": a.arguments,
                    "to": {t: a.arguments.get(t) for t in (tool.targets if tool else ())},
                    "evidence": a.evidence,
                    "agent": s.agent,
                    "session": s.id,
                }
            )
        return cards

    @staticmethod
    def summary(db: Session, session_id: str) -> dict:
        """세션 하나의 숫자(RQ6): 판단별 수, 효과의 끝 상태별 수, 사람이 승인한 수."""
        s = db.get(AgentSessionRow, session_id)
        decisions = Counter(
            db.scalars(
                sa.select(ToolDecisionRow.decision).where(ToolDecisionRow.session_id == session_id)
            ).all()
        )
        actions = db.scalars(
            sa.select(ToolActionRow).where(ToolActionRow.session_id == session_id)
        ).all()
        return {
            "flags": s.flags if s else [],
            "decisions": dict(decisions),
            "actions": dict(Counter(a.status for a in actions)),
            "asked": sum(1 for a in actions if a.decide_by is not None),
            "approved_by_person": sum(
                1 for a in actions if a.approved_by and not a.approved_by.startswith("policy:")
            ),
        }

    # --- 안쪽 ---

    @staticmethod
    def _take(db: Session, action_id: str, frm: tuple[str, ...], to: str) -> bool:
        """상태를 조건부로 바꾼다(한 문장). 그 사이 다른 쪽이 바꿨으면 False."""
        took = db.execute(
            sa.update(ToolActionRow)
            .where(ToolActionRow.id == action_id, ToolActionRow.status.in_(frm))
            .values(status=to)
            .execution_options(synchronize_session=False)
        )
        if took.rowcount == 1:
            db.expire_all()
            return True
        return False

    def _hold(self, tool: Tool | None) -> timedelta:
        return tool.hold if tool is not None and tool.hold is not None else self.hold

    @staticmethod
    def _action(db: Session, action_id: str) -> ToolActionRow:
        a = db.get(ToolActionRow, action_id)
        if a is None:
            raise KeyError(f"없는 효과: {action_id}")
        return a

    @staticmethod
    def _view(a: ToolActionRow) -> dict:
        return {
            "id": a.id,
            "tool": a.tool,
            "status": a.status,
            "policy": a.policy,
            "reason": a.reason,
            "needs_approval": a.status == "held",
            "marks": a.marks,
            "dropped": a.dropped,
            "decide_by": a.decide_by.isoformat() if a.decide_by else None,
            "commit_after": a.commit_after.isoformat() if a.commit_after else None,
        }

    def _close(self, db: Session, a: ToolActionRow, status: str, now: datetime) -> None:
        a.status = status
        self._emit(db, a, db.get(AgentSessionRow, a.session_id), status, now)

    @staticmethod
    def _emit(db: Session, a: ToolActionRow, s: AgentSessionRow | None, kind: str, now) -> None:
        person = s.person_id if s is not None else None
        events.emit(
            db,
            f"action.{kind}",
            {
                "action_id": a.id,
                "session_id": a.session_id,
                "tool": a.tool,
                "policy": a.policy,
                "reason": a.reason,
                "marks": sorted({m["mark"] for m in a.marks}),
            },
            producer=PRODUCER,
            key=f"{a.id}:{kind}",
            ref=events.Ref(subject=f"person:{person}" if person else None, labels={"tool": a.tool}),
            now=now,
        )

    @staticmethod
    def _decide(
        db: Session,
        s: AgentSessionRow,
        tool: str,
        decision: str,
        step: str,
        reason: str | None,
        before: set,
        after: set,
        now: datetime,
        action_id: str | None = None,
    ) -> None:
        db.add(
            ToolDecisionRow(
                session_id=s.id,
                at=now,
                tool=tool[:64],
                decision=decision,
                step=step,
                reason=reason[:300] if reason else None,
                flags_before=_flags(before),
                flags_after=_flags(after),
                action_id=action_id,
            )
        )
        db.flush()
        flight.record_here(
            "tool_decision",
            ref=f"session:{s.id}",
            payload={"tool": tool, "decision": decision, "step": step},
        )

    def _observe(self, tool, args, result, error, started, hidden) -> None:
        if self.observer is None:
            return
        ms = int((time.perf_counter() - started) * 1000) if started is not None else 0
        shown = {k: v for k, v in args.items() if k != INJECTED}
        self.observer.record(
            tool, shown, result=result, error=error, duration_ms=ms, hidden_lines=hidden
        )
