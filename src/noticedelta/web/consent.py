"""베타 개인정보 수집·이용 동의 (작업 목록 19번).

문구(templates/form.html, privacy.html)를 바꾸면 VERSION을 올린다. 프로필에 동의한 판이 남는다.
법률 검토를 받은 문구가 아니다. 공개 운영 전에 다시 본다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import vault
from noticedelta.db.models import JudgmentRow, OutboxAlert, Profile, SubscriptionRow
from noticedelta.events import Ref, app_ref, emit
from noticedelta.grants import APP

VERSION = "beta-2026-11"
KEEP_UNTIL = "2027-03-31"  # 베타가 끝나면 모두 지운다. 일정이 바뀌면 여기만 고친다
ITEMS = (
    "창업일, 대표자 생년월일, 사업장 소재지(시·도), 사업자 형태, 작년 매출, 상시 근로자 수, "
    "예비 창업·여성기업·재창업 여부"
)


PURPOSES = ("judge", "notify")  # 필수 동의의 용도(platform-services 4.3). 선택 동의는 research


def _who(profile: Profile) -> Ref:
    return Ref(app=app_ref(APP.id, APP.version), subject=f"person:{profile.person_id}")


def record(
    session: Session, profile: Profile, *, research_ok: bool, at: datetime, keys: list[str] = ()
) -> None:
    """처음 동의를 남긴다: 금고의 동의(앱·용도·키), 프로필의 문구 판과 시각, 봉투(consent.granted)."""
    profile.consent_version, profile.consented_at = VERSION, at
    profile.research_ok = research_ok
    purposes = [*PURPOSES, "research"] if research_ok else list(PURPOSES)
    for purpose in purposes:
        vault.grant_consent(
            session, profile.person_id, APP.id, purpose, keys, text_version=VERSION, now=at
        )
    emit(session, "consent.granted", {"purpose": purposes, "text_version": VERSION},
         producer="svc:web", key=f"{profile.person_id}:{at.isoformat()}", ref=_who(profile), now=at)  # fmt: skip


def update_keys(session: Session, profile: Profile, keys: list[str], *, at: datetime) -> None:
    """사람이 값을 고쳐 키가 바뀌면, 지금 동의한 용도마다 같은 문구로 키를 새로 적는다."""
    purposes = [*PURPOSES, "research"] if profile.research_ok else list(PURPOSES)
    for purpose in purposes:
        vault.grant_consent(
            session, profile.person_id, APP.id, purpose, keys, text_version=VERSION, now=at
        )


def set_research(session: Session, profile: Profile, ok: bool, *, at: datetime) -> None:
    """선택 동의(연구)를 바꾼다. 바뀌었을 때만 봉투를 낸다."""
    if bool(profile.research_ok) == ok:
        return
    profile.research_ok = ok
    if ok:
        keys = sorted(vault.keys_of(session, profile.person_id))
        vault.grant_consent(
            session, profile.person_id, APP.id, "research", keys, text_version=VERSION, now=at
        )
    else:
        vault.withdraw_consent(session, profile.person_id, APP.id, "research", now=at)
    kind = "consent.granted" if ok else "consent.withdrawn"
    emit(session, kind, {"purpose": ["research"], "text_version": VERSION},
         producer="svc:web", key=f"{profile.person_id}:research:{at.isoformat()}", ref=_who(profile), now=at)  # fmt: skip


def delete_user(session: Session, person_id: str, *, at: datetime | None = None) -> bool:
    """사용자가 남긴 것을 모두 지운다: 구독, 판정 이력, 알림, 프로필. 없던 사용자면 False.

    링크와 사람은 people.forget이 맡는다(사람 ID는 가명으로 남는다).
    """
    if session.get(Profile, person_id) is None:
        return False
    emit(
        session,
        "consent.withdrawn",
        {"purpose": [*PURPOSES, "research"], "text_version": VERSION},
        producer="svc:web",
        key=f"{person_id}:all",
        ref=Ref(app=app_ref(APP.id, APP.version), subject=f"person:{person_id}"),
    )
    for model in (SubscriptionRow, JudgmentRow, OutboxAlert):
        session.execute(sa.delete(model).where(model.person_id == person_id))
    session.execute(sa.delete(Profile).where(Profile.person_id == person_id))
    vault.erase(
        session, person_id, now=at or datetime.now(UTC)
    )  # 값·허가를 지우고 동의는 철회로 남긴다
    return True
