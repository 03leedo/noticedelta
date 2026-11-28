"""보관 기한 정리 (platform-services 4장 "지우기", 4.8의 남은 것): 오래 둘 이유가 없는 것을 지운다.

    p = purge(session, now=지금)                               # 날마다
    p = purge(session, now=지금, beta_ends=KEEP_UNTIL, end_beta=True)   # 베타가 끝나면 모두

- 금고의 값: 보관 기한(expires_at)이 지난 것을 지운다.
- 금고의 허가: 만료되거나 철회된 지 KEEP_GRANTS(30일)가 지난 것을 지운다. 접근 기록(vault_access)은
  남긴다(누가 언제 무엇을 꺼냈는지는 감사 기록이다. 값은 들어 있지 않다).
- 에이전트 효과: 끝난 지 KEEP_ACTIONS(30일)가 지난 효과의 인자·결과(본문에 넣은 내 정보 등)를 지운다.
  행과 상태는 남긴다(RQ6의 숫자가 이것으로 나온다). 반영된 internal 효과의 되돌리기 정보는 남긴다.
- 베타가 끝나면(end_beta, 그날이 지난 뒤에만): 모든 사람의 구독·판정·알림·프로필·금고 값을 지우고 링크를
  끊는다. 사람 ID는 가명으로 남는다(동의 철회 기록과 감사 기록이 가리킨다).

돌리는 것은 운영자다(scripts/retention.py). 미리 세어 보고(기본) --apply로 지운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import managers, people, user_sources, vault
from noticedelta.db.models import (
    AgentSessionRow,
    JudgmentRow,
    OutboxAlert,
    Person,
    Profile,
    SubscriptionRow,
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
    for a in session.scalars(
        sa.select(ToolActionRow).where(
            ToolActionRow.status.in_(FINISHED), ToolActionRow.requested_at <= stale
        )
    ):
        # 끝난 시각으로 센다(요청한 지 오래돼도 끝난 지 얼마 안 됐으면 남긴다)
        ended = a.undone_at or a.committed_at or a.decided_at or a.requested_at
        if _aware(ended) > stale or not (a.arguments or a.result):
            continue  # 아직이거나 이미 지웠다
        a.arguments, a.result = {}, None
        if a.status != "committed":  # 반영된 internal 효과는 되돌릴 수 있게 undo를 남긴다
            a.undo = None
        out.actions += 1
    if end_beta and beta_ends is not None and now.date() > beta_ends:
        gate = ToolGateway([], chosen=[])  # 세션을 닫고 효과의 인자를 지우는 데만 쓴다
        for person_id in sorted(_everyone(session)):
            out.people += _erase(session, gate, person_id, now)
    session.flush()
    return out


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def _everyone(session: Session) -> set[str]:
    """정보가 남아 있을 수 있는 모든 사람: 지우지 않은 사람, 그리고 프로필·금고·구독·판정·세션이
    가리키는 사람(프로필 없이 남은 것도 빠뜨리지 않게)."""
    ids = set(session.scalars(sa.select(Person.id).where(Person.deleted_at.is_(None))).all())
    for col in (
        Profile.person_id,
        VaultValueRow.person_id,
        SubscriptionRow.person_id,
        JudgmentRow.person_id,
        AgentSessionRow.person_id,
    ):
        ids |= {i for i in session.scalars(sa.select(col).distinct()).all() if i}
    return ids


def _erase(session: Session, gate: ToolGateway, person_id: str, now: datetime) -> int:
    """한 사람의 정보를 지운다. 실제로 지운 것이 있으면 1."""
    did = gate.forget_person(session, person_id, now=now) > 0
    did |= managers.forget(session, person_id, at=now) > 0
    did |= user_sources.forget(session, person_id, at=now) > 0
    if not consent.delete_user(session, person_id, at=now):  # 프로필이 없어도 남은 것을 지운다
        for model in (SubscriptionRow, JudgmentRow, OutboxAlert):
            did |= (
                session.execute(sa.delete(model).where(model.person_id == person_id)).rowcount > 0
            )
        did |= bool(vault.keys_of(session, person_id))
        vault.erase(session, person_id, now=now)
    else:
        did = True
    did |= people.forget(session, person_id, at=now)
    return int(did)
