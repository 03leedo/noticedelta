"""앱 정의의 원본은 DB다 (app-spec 15장 "정의의 원본").

    register(session, app, at=)   판 하나를 넣는다. 같은 판이 다른 내용으로 이미 있으면 막는다
    enable(session, app_id, version, at=)   그 판을 켠다(켠 판 = 가장 늦게 켠 판)
    current(session, app_id)      켠 판의 정의

우리 앱과 운영자 출처는 저장소에 파일로 써 두고 시작할 때 넣는다(씨앗). 사용자가 만드는 앱도 같은
길(register → 미리 돌려 보기 → 사람의 확인 → enable)을 간다. 켜는 일은 사람의 확인이 필요한
순간이다(app-spec 3장). 씨앗은 운영자가 저장소에서 확인한 것이라 바로 켠다.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.appspec import AppDefinition
from noticedelta.db.models import AppBudgetRow, AppDefinitionRow, AppMemberRow
from noticedelta.events import Ref, app_ref, emit


class VersionConflict(ValueError):
    """같은 판 번호에 다른 내용. 켠 판은 바꾸지 않는다(app-spec 9장)."""


def register(session: Session, app: AppDefinition, *, at: datetime) -> AppDefinitionRow:
    digest = app.content_hash()
    row = session.get(AppDefinitionRow, (app.id, app.version))
    if row is not None:
        if row.content_hash != digest:
            raise VersionConflict(
                f"{app.id} 판 {app.version}은 이미 다른 내용으로 있다. "
                f"정의를 고쳤으면 version을 {app.version + 1}로 올린다"
            )
        return row
    row = AppDefinitionRow(
        app_id=app.id,
        version=app.version,
        content_hash=digest,
        body=app.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        created_at=at,
    )
    session.add(row)
    session.flush()
    return row


def enable(session: Session, app_id: str, version: int, *, at: datetime) -> None:
    row = session.get(AppDefinitionRow, (app_id, version))
    if row is None:
        raise KeyError(f"{app_id} 판 {version}이 없다. 먼저 register한다")
    if row.enabled_at is None or row != _current_row(session, app_id):
        row.enabled_at = at
        emit(
            session,
            "app.version_enabled",
            {"app_id": app_id, "version": version, "content_hash": row.content_hash},
            producer="svc:apps",
            key=f"{app_id}@{version}:{at.isoformat()}",
            ref=Ref(app=app_ref(app_id, version)),
            now=at,
        )


def seed(session: Session, app: AppDefinition, *, at: datetime) -> AppDefinitionRow:
    """저장소의 정의 파일을 넣고, 켠 판이 없거나 더 낮으면 켠다."""
    row = register(session, app, at=at)
    cur = _current_row(session, app.id)
    if cur is None or cur.version < app.version:
        enable(session, app.id, app.version, at=at)
    return row


def _current_row(session: Session, app_id: str) -> AppDefinitionRow | None:
    return session.scalars(
        sa.select(AppDefinitionRow)
        .where(AppDefinitionRow.app_id == app_id, AppDefinitionRow.enabled_at.is_not(None))
        .order_by(AppDefinitionRow.enabled_at.desc(), AppDefinitionRow.version.desc())
        .limit(1)
    ).first()


def current(session: Session, app_id: str) -> AppDefinition | None:
    row = _current_row(session, app_id)
    return AppDefinition.model_validate(row.body) if row is not None else None


def enabled(session: Session) -> list[AppDefinition]:
    """켠 판이 있는 앱마다 그 판의 정의(app_id 순)."""
    ids = session.scalars(
        sa.select(AppDefinitionRow.app_id)
        .where(AppDefinitionRow.enabled_at.is_not(None))
        .distinct()
        .order_by(AppDefinitionRow.app_id)
    ).all()
    return [a for a in (current(session, i) for i in ids) if a is not None]


# --- 사람과 역할 (app-spec 6장) ---

OWNER, MANAGER = "owner", "manager"


def add_member(session: Session, app_id: str, person_id: str, role: str, *, at: datetime) -> None:
    if role not in (OWNER, MANAGER):
        raise ValueError(f"모르는 역할: {role}")
    row = session.get(AppMemberRow, (app_id, person_id, role))
    if row is None:
        session.add(AppMemberRow(app_id=app_id, person_id=person_id, role=role, added_at=at))
    elif row.removed_at is not None:
        row.added_at, row.removed_at = at, None
    session.flush()


def roles_of(session: Session, app_id: str, person_id: str | None) -> set[str]:
    if person_id is None:
        return set()
    return set(
        session.scalars(
            sa.select(AppMemberRow.role).where(
                AppMemberRow.app_id == app_id,
                AppMemberRow.person_id == person_id,
                AppMemberRow.removed_at.is_(None),
            )
        ).all()
    )


def exists(session: Session, app_id: str) -> bool:
    return session.scalar(sa.select(sa.func.count()).where(AppDefinitionRow.app_id == app_id)) > 0


def drop_draft(session: Session, app_id: str, version: int) -> bool:
    """켜지 않은 판을 지운다(초안 저장 되돌리기). 켠 판은 지우지 않는다."""
    row = session.get(AppDefinitionRow, (app_id, version))
    if row is None or row.enabled_at is not None:
        return False
    session.delete(row)
    session.flush()
    return True


# --- 예산 (app-spec 9장) ---

DEFAULT_DAILY_TOKENS = 20_000  # 다른 앱의 기본 하루 한도. 무료 한도를 나눠 쓰므로 작게 시작한다


def usage_ref(app_id: str, primary: str) -> str:
    """워커가 남기는 LLM 사용 기록(llm_usage)의 ref. 주 앱은 예전 그대로 "worker"다."""
    return "worker" if app_id == primary else f"app:{app_id}"


def daily_tokens(session: Session, app_id: str) -> int | None:
    row = session.get(AppBudgetRow, app_id)
    return row.daily_tokens if row is not None else None


def set_daily_tokens(
    session: Session, app_id: str, tokens: int, *, by: str, at: datetime
) -> int | None:
    """한도를 바꾼다. 앞의 한도(없으면 None)를 준다."""
    if tokens < 0:
        raise ValueError("한도는 0 이상이다")
    row = session.get(AppBudgetRow, app_id)
    before = row.daily_tokens if row is not None else None
    if row is None:
        session.add(AppBudgetRow(app_id=app_id, daily_tokens=tokens, set_by=by[:64], set_at=at))
    else:
        row.daily_tokens, row.set_by, row.set_at = tokens, by[:64], at
    session.flush()
    return before
