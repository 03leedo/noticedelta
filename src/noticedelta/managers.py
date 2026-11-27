"""관리자 (app-spec 6장): 기관이 구독자 여럿의 정보를 대신 관리한다. 구독자의 동의가 있어야 한다.

    grant(s, app_id, person_id, label="부산창업지원단", at=now)   # 운영자가 확인하고 관리자로 둔다
    token = invite(s, app_id, manager_id, at=now)                  # 관리자가 초대 링크를 만든다
    accept(s, token, person_id, at=now)                            # 구독자가 받아들인다(동의)
    read(s, cipher, app_id, manager_id, person_id, now=now)        # 관리자가 그 사람의 값을 본다
    revoke(s, app_id, manager_id, person_id, at=now)               # 구독자가 끊는다

- 관리자는 운영자가 확인해 이름과 함께 둔다(사칭 방지). 초대에 보이는 이름은 그 확인된 이름이다.
- 동의는 금고의 manage 용도로 남는다. 연결(manage_link)마다 받아들인 때의 키를 따로 적고, 관리자는
  그 키와 지금 동의의 키가 겹치는 만큼만 본다. 뒤에 다른 관리자를 받아들여 동의가 넓어져도 앞 관리자가
  보는 키는 넓어지지 않는다.
- 끊으면 그 관리자의 허가만 거둔다. 그 앱의 다른 관리자가 없으면 manage 동의도 철회한다. 동의를 주고
  거둔 일은 봉투(consent.granted/withdrawn)로 남긴다.
- 관리자와 구독자는 서로의 링크를 모른다.
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
from noticedelta.events import Ref, emit
from noticedelta.people import hash_token

PURPOSE = "manage"
INVITE_TTL = timedelta(days=7)
PRODUCER = "svc:managers"


class NotLinked(vault.OutsideConsent):
    """연결이 없거나 끊긴 관리자."""


class NotAManager(PermissionError):
    """운영자가 관리자로 확인하지 않은 사람."""


def _aware(t: datetime, like: datetime) -> datetime:
    return t.replace(tzinfo=like.tzinfo) if t.tzinfo is None else t


def _event(session: Session, kind: str, person_id: str, app_id: str, key: str, at: datetime,
           text_version: str | None = None) -> None:  # fmt: skip
    emit(session, kind, {"purpose": [PURPOSE], "text_version": text_version}, producer=PRODUCER,
         key=key, ref=Ref(subject=f"person:{person_id}", labels={"app": app_id}), now=at)  # fmt: skip


def grant(session: Session, app_id: str, person_id: str, *, label: str, at: datetime) -> None:
    """운영자가 그 사람을 관리자로 둔다(기관을 확인한 뒤). 구독자에게는 이 이름이 보인다."""
    label = " ".join(label.split())[:50]
    if not label:
        raise ValueError("구독자에게 보일 이름(label)이 필요하다")
    app_registry.add_member(session, app_id, person_id, app_registry.MANAGER, at=at, label=label)


def invite(session: Session, app_id: str, manager_id: str, *, at: datetime,
           ttl: timedelta = INVITE_TTL) -> str:  # fmt: skip
    """초대 링크의 토큰(원문은 이때 한 번만). 기한 안에는 여러 사람이 받을 수 있다."""
    label = app_registry.manager_label(session, app_id, manager_id)
    if label is None:
        raise NotAManager("운영자가 확인한 관리자만 초대를 만든다")
    token = secrets.token_urlsafe(24)
    session.add(ManageInviteRow(token_hash=hash_token(token), app_id=app_id, manager_id=manager_id,
                                label=label, created_at=at, expires_at=at + ttl))  # fmt: skip
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
    """구독자가 초대를 받아들인다: 지금 넣은 키에 manage 동의를 남기고, 그 키를 연결에 적는다."""
    inv = peek(session, token, at=at)
    if inv is None:
        raise ValueError("없거나 기한이 지난 초대")
    if inv.manager_id == person_id:
        raise ValueError("자기 자신을 관리자로 둘 수 없다")
    keys = sorted(vault.keys_of(session, person_id))
    current = vault.active_consent(session, person_id, inv.app_id, PURPOSE)
    wanted = sorted(set(keys) | set(current.keys if current is not None else ()))
    if current is None or sorted(current.keys) != wanted:
        vault.grant_consent(session, person_id, inv.app_id, PURPOSE, wanted,
                            text_version=text_version, now=at)  # fmt: skip
    link = session.get(ManageLinkRow, (inv.app_id, inv.manager_id, person_id))
    if link is None:
        link = ManageLinkRow(app_id=inv.app_id, manager_id=inv.manager_id, person_id=person_id,
                             label=inv.label, keys=keys, created_at=at)  # fmt: skip
        session.add(link)
    else:
        link.revoked_at, link.label, link.keys, link.created_at = None, inv.label, keys, at
    _event(session, "consent.granted", person_id, inv.app_id,
           f"{person_id}:manage:{inv.manager_id}:{at.isoformat()}", at, text_version)  # fmt: skip
    session.flush()
    return link


def _active(session: Session, app_id: str, manager_id: str, person_id: str) -> ManageLinkRow:
    link = session.get(ManageLinkRow, (app_id, manager_id, person_id))
    if link is None or link.revoked_at is not None:
        raise NotLinked("이 사람을 관리하도록 동의받지 않았다")
    return link


def _allowed(session: Session, link: ManageLinkRow) -> list[str]:
    """그 관리자가 쓸 수 있는 키: 받아들인 때의 키 ∩ 지금 동의의 키(구독자가 줄였으면 줄어든다)."""
    consent = vault.active_consent(session, link.person_id, link.app_id, PURPOSE)
    if consent is None:
        raise NotLinked("관리 동의가 없다")
    return sorted(set(link.keys) & set(consent.keys))


def _close(session: Session, link: ManageLinkRow, *, at: datetime) -> None:
    """연결 하나를 닫는다. 그 앱의 다른 관리자가 없으면 동의를 철회하고, 있으면 이 관리자의 허가만 거둔다."""
    link.revoked_at = at
    session.flush()
    others = session.scalar(
        sa.select(sa.func.count()).where(
            ManageLinkRow.app_id == link.app_id,
            ManageLinkRow.person_id == link.person_id,
            ManageLinkRow.revoked_at.is_(None),
        )
    )
    if not others:
        vault.withdraw_consent(session, link.person_id, link.app_id, PURPOSE, now=at)
    else:
        session.execute(
            sa.update(VaultGrantRow)
            .where(
                VaultGrantRow.person_id == link.person_id,
                VaultGrantRow.app_id == link.app_id,
                VaultGrantRow.purpose == PURPOSE,
                VaultGrantRow.holder == f"manager:{link.manager_id}",
                VaultGrantRow.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
    _event(session, "consent.withdrawn", link.person_id, link.app_id,
           f"{link.person_id}:manage:{link.manager_id}:{at.isoformat()}", at)  # fmt: skip


def revoke(session: Session, app_id: str, manager_id: str, person_id: str, *, at: datetime) -> bool:
    """구독자가 그 관리자를 끊는다."""
    link = session.get(ManageLinkRow, (app_id, manager_id, person_id))
    if link is None or link.revoked_at is not None:
        return False
    _close(session, link, at=at)
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
    """관리자가 그 사람의 값을 본다(그 연결에 맡긴 키만, 꺼낼 때마다 접근 기록)."""
    keys = _allowed(session, _active(session, app_id, manager_id, person_id))
    if not keys:
        return {}
    grant_row = vault.issue(session, person_id, app_id, PURPOSE, keys,
                            holder=f"manager:{manager_id}", now=now)  # fmt: skip
    return vault.read(session, cipher, grant_row.id, now=now)


def write(session: Session, cipher: vault.Cipher, app_id: str, manager_id: str, person_id: str,
          values: Mapping[str, Any], *, now: datetime) -> None:  # fmt: skip
    """관리자가 그 사람의 값을 고친다. 그 연결에 맡긴 키 가운데 지금 금고에 있는 것만(지운 값을 되살리지 않게)."""
    keys = set(_allowed(session, _active(session, app_id, manager_id, person_id)))
    keys &= vault.keys_of(session, person_id)
    outside = set(values) - keys
    if outside:
        raise vault.OutsideConsent(f"맡기지 않았거나 지금 없는 키: {sorted(outside)}")
    vault.put(session, cipher, person_id, values, source="manager", now=now)


def forget(session: Session, person_id: str, *, at: datetime) -> int:
    """사람이 정보를 지울 때: 그 사람이 맡긴 연결과 맡은 연결을 모두 닫고(동의 철회 규칙 그대로),
    그 사람의 초대를 닫는다. 닫은 연결 수."""
    links = session.scalars(
        sa.select(ManageLinkRow).where(
            sa.or_(ManageLinkRow.person_id == person_id, ManageLinkRow.manager_id == person_id),
            ManageLinkRow.revoked_at.is_(None),
        )
    ).all()
    for link in links:
        _close(session, link, at=at)
    session.execute(
        sa.update(ManageInviteRow)
        .where(ManageInviteRow.manager_id == person_id, ManageInviteRow.expires_at > at)
        .values(expires_at=at)
    )
    return len(links)
