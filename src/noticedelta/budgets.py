"""앱별 LLM 예산 (app-spec 9장 "비용과 한도"): 앱마다 하루 한도를 두고, 그 위에 하루 전체 한도를 건다.

    root, per_app = plan(session, apps, primary=APP.id, daily=200_000, since=오늘 0시)

- 하루 전체 한도(daily)는 무료 공급자의 하루 몫이다. 앱들이 나눠 쓴다. 다 쓰면 모든 앱이 멈춘다.
- 앱 한도: 주 앱은 따로 정하지 않았으면 하루 전체 한도와 같다. 다른 앱은 소유자가 정한 값(app_budget),
  없으면 기본값(작게)이다. 앱 한도를 다 쓰면 그 앱의 영향 계산만 미뤄진다(수집과 출처 값 필드는 계속).
- 다른 앱들은 하루 전체 한도의 OTHERS_SHARE(30%)를 함께 나눠 쓴다(공동 몫). 소유자가 자기 한도를
  얼마로 올려도 주 앱의 몫(70%)을 먹지 못한다. 공동 몫을 넓히는 것은 운영자의 일이다.
- 오늘 쓴 양은 워커가 남기는 사용 기록(llm_usage)에서 센다. 앱마다 ref가 다르다(app_registry.usage_ref).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.orm import Session

from noticedelta import app_registry, flight
from noticedelta.appspec import AppDefinition
from noticedelta.llm.gateway import Budget

OTHERS_SHARE = 0.3  # 다른 앱들이 함께 쓰는 하루 전체 한도의 몫


def others_cap(daily: int) -> int:
    return int(daily * OTHERS_SHARE)


def cap_of(session: Session, app_id: str, *, primary: str, daily: int) -> int:
    """앱의 하루 한도."""
    set_ = app_registry.daily_tokens(session, app_id)
    if app_id == primary:
        return set_ if set_ is not None else daily
    # 다른 앱은 공동 몫을 넘지 못한다(DB에 더 큰 값이 있어도)
    return min(set_ if set_ is not None else app_registry.DEFAULT_DAILY_TOKENS, others_cap(daily))


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
    total = flight.spent_tokens(session, since)
    root = Budget(max(daily - total, 0))
    mine = spent_today(session, primary, primary=primary, since=since)
    others = Budget(max(others_cap(daily) - (total - mine), 0), parent=root)  # 다른 앱의 공동 몫
    per_app = {}
    for app in apps:
        cap = cap_of(session, app.id, primary=primary, daily=daily)
        used = spent_today(session, app.id, primary=primary, since=since)
        parent = root if app.id == primary else others
        per_app[app.id] = Budget(max(cap - used, 0), parent=parent)
    return root, per_app


def budget_for(
    session: Session, app_id: str, *, primary: str, daily: int, since: datetime, root: Budget
) -> Budget:
    """워커가 돌다가 새로 켜진 앱의 예산(공동 몫은 지금 남은 만큼으로 다시 센다)."""
    total = flight.spent_tokens(session, since)
    mine = spent_today(session, primary, primary=primary, since=since)
    others = Budget(max(others_cap(daily) - (total - mine), 0), parent=root)
    cap = cap_of(session, app_id, primary=primary, daily=daily)
    used = spent_today(session, app_id, primary=primary, since=since)
    return Budget(max(cap - used, 0), parent=others)
