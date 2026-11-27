"""관리자 (app-spec 6장): 기관이 구독자 여럿의 정보를 대신 관리한다. 구독자의 동의가 있어야 한다.

    token = invite(s, app_id, manager_id, label="부산창업지원단", at=now)   # 관리자가 초대 링크를 만든다
    accept(s, token, person_id, at=now)                                    # 구독자가 받아들인다(동의)
    read(s, cipher, app_id, manager_id, person_id, now=now)                # 관리자가 그 사람의 값을 본다
    revoke(s, app_id, manager_id, person_id, at=now)                       # 구독자가 끊는다

- 동의는 금고의 manage 용도로 남긴다(구독자가 지금 넣은 키에). 연결(manage_link)이 살아 있는 관리자만
  그 동의로 허가를 받는다(holder=manager:<id>, 한 번 쓰는 허가, 꺼낼 때마다 접근 기록).
- 관리자가 값을 고칠 수 있는 것도 동의한 키뿐이다.
- 끊으면 그 관리자의 연결만 닫는다. 그 앱의 다른 관리자가 없으면 manage 동의도 철회한다(허가가 바로 무효).
- 관리자와 구독자는 서로의 링크를 모른다. 구독자에게는 초대를 만들 때 적은 이름(label)만 보인다.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import app_registry, vault
from noticedelta.db.models import ManageInviteRow, ManageLinkRow, VaultGrantRow
from noticedelta.people import hash_token

PURPOSE = "manage"
INVITE_TTL = timedelta(days=7)


class NotLinked(vault.OutsideConsent):
    """연결이 없거나 끊긴 관리자."""


def _aware(t: datetime, like: datetime) -> datetime:
    return t.replace(tzinfo=like.tzinfo) if t.tzinfo is None else t


def invite(
    session: Session, app_id: str, manager_id: str, *, label: str, at: datetime,
    ttl: timedelta = INVITE_TTL,
) -> str:  # fmt: skip
    """초대 링크의 토큰(원문은 이때 한 번만). 기한 안에는 여러 사람이 받을 수 있다."""
    label = " ".join(label.split())[:50]
    if not label:
        raise ValueError("구독자에게 보일 이름(label)이 필요하다")
    token = secrets.token_urlsafe(24)
    session.add(ManageInviteRow(token_hash=hash_token(token), app_id=app_id, manager_id=manager_id,
                                label=label, created_at=at, expires_at=at + ttl))  # fmt: skip
    app_registry.add_member(session, app_id, manager_id, app_registry.MANAGER, at=at)
    session.flush()
    return token


def peek(session: Session, token: str, *, at: datetime) -> ManageInviteRow | None:
    """살아 있는 초대(없거나 기한이 지났으면 None)."""
    if not token or len(token) > 128:
        return None
    row = session.get(ManageInviteRow, hash_token(token))
    if row is None or _aware(row.expires_at, at) <= at:
        return None
    return row


def accept(session: Session, token: str, person_id: str, *, at: datetime,
           text_version: str | None = None) -> ManageLinkRow:  # fmt: skip
    """구독자가 초대를 받아들인다: 지금 넣은 키에 manage 동의를 남기고 연결을 만든다."""
    inv = peek(session, token, at=at)
    if inv is None:
        raise ValueError("없거나 기한이 지난 초대")
    if inv.manager_id == person_id:
        raise ValueError("자기 자신을 관리자로 둘 수 없다")
    keys = sorted(vault.keys_of(session, person_id))
    current = vault.active_consent(session, person_id, inv.app_id, PURPOSE)
    if current is None or sorted(current.keys) != keys:
        vault.grant_consent(session, person_id, inv.app_id, PURPOSE, keys,
                            text_version=text_version, now=at)  # fmt: skip
    link = session.get(ManageLinkRow, (inv.app_id, inv.manager_id, person_id))
    if link is None:
        link = ManageLinkRow(app_id=inv.app_id, manager_id=inv.manager_id, person_id=person_id,
                             label=inv.label, created_at=at)  # fmt: skip
        session.add(link)
    else:
        link.revoked_at, link.label, link.created_at = None, inv.label, at
    session.flush()
    return link


def _active(session: Session, app_id: str, manager_id: str, person_id: str) -> ManageLinkRow:
    link = session.get(ManageLinkRow, (app_id, manager_id, person_id))
    if link is None or link.revoked_at is not None:
        raise NotLinked("이 사람을 관리하도록 동의받지 않았다")
    return link


def revoke(session: Session, app_id: str, manager_id: str, person_id: str, *, at: datetime) -> bool:
    """구독자가 그 관리자를 끊는다. 그 앱의 다른 관리자가 없으면 manage 동의도 철회한다."""
    link = session.get(ManageLinkRow, (app_id, manager_id, person_id))
    if link is None or link.revoked_at is not None:
        return False
    link.revoked_at = at
    session.flush()
    others = session.scalar(
        sa.select(sa.func.count()).where(
            ManageLinkRow.app_id == app_id,
            ManageLinkRow.person_id == person_id,
            ManageLinkRow.revoked_at.is_(None),
        )
    )
    if not others:
        vault.withdraw_consent(session, person_id, app_id, PURPOSE, now=at)
    else:  # 다른 관리자는 남는다: 끊긴 관리자가 이미 받은 허가만 거둔다
        session.execute(
            sa.update(VaultGrantRow)
            .where(
                VaultGrantRow.person_id == person_id,
                VaultGrantRow.holder == f"manager:{manager_id}",
                VaultGrantRow.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
    return True


def managed(session: Session, manager_id: str, app_id: str | None = None) -> list[ManageLinkRow]:
    q = sa.select(ManageLinkRow).where(
        ManageLinkRow.manager_id == manager_id, ManageLinkRow.revoked_at.is_(None)
    )
    if app_id is not None:
        q = q.where(ManageLinkRow.app_id == app_id)
    return list(session.scalars(q.order_by(ManageLinkRow.created_at)).all())


def managers_of(session: Session, person_id: str) -> list[ManageLinkRow]:
    return list(
        session.scalars(
            sa.select(ManageLinkRow).where(
                ManageLinkRow.person_id == person_id, ManageLinkRow.revoked_at.is_(None)
            )
        ).all()
    )


def read(session: Session, cipher: vault.Cipher, app_id: str, manager_id: str, person_id: str,
         *, now: datetime) -> dict[str, Any]:  # fmt: skip
    """관리자가 그 사람의 값을 본다(동의한 키만, 꺼낼 때마다 접근 기록)."""
    _active(session, app_id, manager_id, person_id)
    consent = vault.active_consent(session, person_id, app_id, PURPOSE)
    if consent is None:
        raise NotLinked("관리 동의가 없다")
    grant = vault.issue(session, person_id, app_id, PURPOSE, consent.keys,
                        holder=f"manager:{manager_id}", now=now)  # fmt: skip
    return vault.read(session, cipher, grant.id, now=now)


def write(session: Session, cipher: vault.Cipher, app_id: str, manager_id: str, person_id: str,
          values: Mapping[str, Any], *, now: datetime) -> None:  # fmt: skip
    """관리자가 그 사람의 값을 고친다. 동의한 키만."""
    _active(session, app_id, manager_id, person_id)
    consent = vault.active_consent(session, person_id, app_id, PURPOSE)
    outside = set(values) - set(consent.keys if consent else ())
    if outside:
        raise vault.OutsideConsent(f"동의하지 않은 키: {sorted(outside)}")
    vault.put(session, cipher, person_id, values, source="manager", now=now)


def forget(session: Session, person_id: str, *, at: datetime) -> int:
    """사람이 정보를 지울 때: 그 사람이 맡긴 연결과 그 사람이 맡은 연결, 그 사람의 초대를 모두 닫는다."""
    n = session.execute(
        sa.update(ManageLinkRow)
        .where(
            sa.or_(ManageLinkRow.person_id == person_id, ManageLinkRow.manager_id == person_id),
            ManageLinkRow.revoked_at.is_(None),
        )
        .values(revoked_at=at)
    ).rowcount
    session.execute(
        sa.update(ManageInviteRow)
        .where(ManageInviteRow.manager_id == person_id, ManageInviteRow.expires_at > at)
        .values(expires_at=at)
    )
    return n
