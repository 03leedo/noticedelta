"""베타 개인정보 수집·이용 동의 (작업 목록 19번).

문구(templates/form.html, privacy.html)를 바꾸면 VERSION을 올린다. 프로필에 동의한 판이 남는다.
법률 검토를 받은 문구가 아니다. 공개 운영 전에 다시 본다.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import JudgmentRow, OutboxAlert, Profile, SubscriptionRow

VERSION = "beta-2026-11"
KEEP_UNTIL = "2027-03-31"  # 베타가 끝나면 모두 지운다. 일정이 바뀌면 여기만 고친다
ITEMS = (
    "창업일, 대표자 생년월일, 사업장 소재지(시·도), 사업자 형태, 작년 매출, 상시 근로자 수, "
    "예비 창업·여성기업·재창업 여부"
)


def record(profile: Profile, *, research_ok: bool, at: datetime) -> None:
    profile.consent_version, profile.consented_at = VERSION, at
    profile.research_ok = research_ok


def delete_user(session: Session, user_id: str) -> bool:
    """사용자가 남긴 것을 모두 지운다: 구독, 판정 이력, 알림, 프로필. 없던 사용자면 False."""
    if session.get(Profile, user_id) is None:
        return False
    for model in (SubscriptionRow, JudgmentRow, OutboxAlert):
        session.execute(sa.delete(model).where(model.user_id == user_id))
    session.execute(sa.delete(Profile).where(Profile.user_id == user_id))
    return True
