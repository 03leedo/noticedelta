"""앱별 LLM 예산 (app-spec 9장 "비용과 한도"): 앱마다 하루 한도를 두고, 그 위에 하루 전체 한도를 건다.

    root, per_app = plan(session, apps, primary=APP.id, daily=200_000, since=오늘 0시)

- 하루 전체 한도(daily)는 무료 공급자의 하루 몫이다. 앱들이 나눠 쓴다. 다 쓰면 모든 앱이 멈춘다.
- 앱 한도: 주 앱은 따로 정하지 않았으면 하루 전체 한도와 같다. 다른 앱은 소유자가 정한 값(app_budget),
  없으면 기본값(작게)이다. 앱 한도를 다 쓰면 그 앱의 영향 계산만 미뤄진다(수집과 출처 값 필드는 계속).
- 오늘 쓴 양은 워커가 남기는 사용 기록(llm_usage)에서 센다. 앱마다 ref가 다르다(app_registry.usage_ref).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.orm import Session

from noticedelta import app_registry, flight
from noticedelta.appspec import AppDefinition
from noticedelta.llm.gateway import Budget


def cap_of(session: Session, app_id: str, *, primary: str, daily: int) -> int:
    """앱의 하루 한도."""
    set_ = app_registry.daily_tokens(session, app_id)
    if set_ is not None:
        return set_
    return daily if app_id == primary else app_registry.DEFAULT_DAILY_TOKENS


def spent_today(session: Session, app_id: str, *, primary: str, since: datetime) -> int:
    return flight.spent_tokens(session, since, refs=[app_registry.usage_ref(app_id, primary)])


def plan(
    session: Session,
    apps: Iterable[AppDefinition],
    *,
    primary: str,
    daily: int,
    since: datetime,
) -> tuple[Budget, dict[str, Budget]]:
    root = Budget(max(daily - flight.spent_tokens(session, since), 0))
    per_app = {}
    for app in apps:
        cap = cap_of(session, app.id, primary=primary, daily=daily)
        used = spent_today(session, app.id, primary=primary, since=since)
        per_app[app.id] = Budget(max(cap - used, 0), parent=root)
    return root, per_app
