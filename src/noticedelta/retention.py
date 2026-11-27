"""보관 기한 정리 (platform-services 4장 "지우기", 4.8의 남은 것): 오래 둘 이유가 없는 것을 지운다.

    p = purge(session, now=지금)                               # 날마다
    p = purge(session, now=지금, beta_ends=KEEP_UNTIL, end_beta=True)   # 베타가 끝나면 모두

- 금고의 값: 보관 기한(expires_at)이 지난 것을 지운다.
- 금고의 허가: 만료되거나 철회된 지 KEEP_GRANTS(30일)가 지난 것을 지운다. 접근 기록(vault_access)은
  남긴다(누가 언제 무엇을 꺼냈는지는 감사 기록이다. 값은 들어 있지 않다).
- 에이전트 효과: 끝난 지 KEEP_ACTIONS(30일)가 지난 효과의 인자·결과(본문에 넣은 내 정보 등)를 지운다.
  행과 상태는 남긴다(RQ6의 숫자가 이것으로 나온다).
- 베타가 끝나면(end_beta, 그날이 지난 뒤에만): 모든 사람의 구독·판정·알림·프로필·금고 값을 지우고 링크를
  끊는다. 사람 ID는 가명으로 남는다(동의 철회 기록과 감사 기록이 가리킨다).

돌리는 것은 운영자다(scripts/retention.py). 미리 세어 보고(기본) --apply로 지운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import people
from noticedelta.db.models import (
    AgentSessionRow,
    Profile,
    ToolActionRow,
    VaultGrantRow,
    VaultValueRow,
)
from noticedelta.tool_gateway import ToolGateway
from noticedelta.web import consent

KEEP_GRANTS = timedelta(days=30)
KEEP_ACTIONS = timedelta(days=30)
FINISHED = ("committed", "discarded", "expired", "blocked", "failed", "undone")


@dataclass
class Purged:
    vault_values: int = 0
    grants: int = 0
    actions: int = 0
    people: int = 0


def purge(
    session: Session,
    *,
    now: datetime,
    keep_grants: timedelta = KEEP_GRANTS,
    keep_actions: timedelta = KEEP_ACTIONS,
    beta_ends: date | None = None,
    end_beta: bool = False,
) -> Purged:
    out = Purged()
    out.vault_values = session.execute(
        sa.delete(VaultValueRow).where(
            VaultValueRow.expires_at.is_not(None), VaultValueRow.expires_at <= now
        )
    ).rowcount
    old = now - keep_grants
    out.grants = session.execute(
        sa.delete(VaultGrantRow).where(
            sa.or_(VaultGrantRow.expires_at <= old, VaultGrantRow.revoked_at <= old)
        )
    ).rowcount
    stale = now - keep_actions
    rows = session.execute(
        sa.select(ToolActionRow.id, ToolActionRow.arguments, ToolActionRow.result).where(
            ToolActionRow.status.in_(FINISHED), ToolActionRow.requested_at <= stale
        )
    ).all()
    ids = [i for i, args, result in rows if args or result]  # 이미 지운 것은 건너뛴다
    if ids:
        out.actions = session.execute(
            sa.update(ToolActionRow)
            .where(ToolActionRow.id.in_(ids))
            .values(arguments={}, result=None, undo=None)
            .execution_options(synchronize_session=False)
        ).rowcount
    if end_beta and beta_ends is not None and now.date() > beta_ends:
        gate = ToolGateway([], chosen=[])  # 세션을 닫고 효과의 인자를 지우는 데만 쓴다
        for person_id in session.scalars(sa.select(Profile.person_id)).all():
            gate.forget_person(session, person_id, now=now)
            consent.delete_user(session, person_id, at=now)
            people.forget(session, person_id, at=now)
            out.people += 1
        # 프로필이 없는 사람의 에이전트 세션도 닫는다
        for (pid,) in session.execute(
            sa.select(AgentSessionRow.person_id)
            .where(AgentSessionRow.person_id.is_not(None), AgentSessionRow.status == "open")
            .distinct()
        ).all():
            gate.forget_person(session, pid, now=now)
    session.flush()
    return out
