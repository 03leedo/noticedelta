"""사람 ID와 개인 링크 (platform-services 1.1, 4.2, 8장 1번).

개인 링크의 토큰은 비밀번호와 같다. DB에는 토큰의 해시만 두고, 판정·구독·알림은 사람 ID(가명)를
쓴다. 예전에는 토큰 원문이 곧 사용자 ID라 DB 곳곳(판정, 구독, 알림의 멱등 키와 본문)에 평문으로
퍼져 있었다. DB 덤프나 백업, 로그 하나만 새도 남의 링크로 들어갈 수 있었다.

토큰은 추측할 수 없게 길어서(18바이트 무작위) 느린 해시(bcrypt 등)가 필요 없다. sha256으로
충분하고, 요청마다 찾을 수 있다.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import AccessLink, Person

TOKEN_BYTES = 18
TOUCH_EVERY = timedelta(days=1)  # 마지막 사용 시각은 하루에 한 번만 고친다(읽기마다 쓰지 않게)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(session: Session, *, at: datetime) -> tuple[Person, str]:
    """사람 하나와 그 사람의 링크를 만든다. 토큰 원문은 여기서 한 번만 돌려준다."""
    person = Person(created_at=at)
    session.add(person)
    session.flush()
    return person, add_link(session, person.id, at=at)


def add_link(session: Session, person_id: str, *, at: datetime, token: str | None = None) -> str:
    """사람에게 링크를 하나 더 낸다(잃어버린 링크 다시 받기, 시험). 토큰을 돌려준다."""
    token = token or secrets.token_urlsafe(TOKEN_BYTES)
    session.add(AccessLink(token_hash=hash_token(token), person_id=person_id, created_at=at))
    return token


def resolve(session: Session, token: str, *, at: datetime | None = None) -> str | None:
    """링크 토큰으로 사람 ID를 찾는다. 없거나 끊긴 링크, 지운 사람이면 None.

    at을 주면 마지막 사용 시각을 고친다(바뀌었으면 부른 쪽이 commit한다).
    """
    if not token or len(token) > 128:
        return None
    row = session.execute(
        sa.select(AccessLink, Person.deleted_at)
        .join(Person, Person.id == AccessLink.person_id)
        .where(AccessLink.token_hash == hash_token(token))
    ).first()
    if row is None:
        return None
    link, deleted_at = row
    if link.revoked_at is not None or deleted_at is not None:
        return None
    if at is not None and (
        link.last_used_at is None or at - _aware(link.last_used_at, at) >= TOUCH_EVERY
    ):
        link.last_used_at = at
    return link.person_id


def forget(session: Session, person_id: str, *, at: datetime) -> bool:
    """링크를 모두 지우고 사람에 지운 시각을 남긴다. 사람 ID는 가명으로 남는다(4.6)."""
    person = session.get(Person, person_id)
    if person is None or person.deleted_at is not None:
        return False
    session.execute(sa.delete(AccessLink).where(AccessLink.person_id == person_id))
    person.deleted_at = at
    return True


def _aware(t: datetime, like: datetime) -> datetime:
    # SQLite는 시간대를 저장하지 않아 읽으면 naive다. 비교하는 쪽의 시간대를 붙인다.
    return t if t.tzinfo is not None or like.tzinfo is None else t.replace(tzinfo=like.tzinfo)
