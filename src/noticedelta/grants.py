"""지원사업 팩: 지원사업 앱이 쓰는 도메인 지식을 한곳에 모은다.

플랫폼(공통·엔진)은 지원사업을 모른다. 여기서 넘겨준다: 필드 정의, 필드별 낱말, 자격 속성 사전.
출처 지식은 어댑터(sources/bizinfo.py, kstartup.py)에 있다.

2026-11-24부터 이 값들은 앱 정의 한 장(apps/grant-alerts.json, 규격은 appspec.py)에서 읽는다.
이 모듈은 정의를 읽어 예전 이름으로 내놓는 얇은 층이다. 고칠 때는 JSON을 고치고 판(version)을 올린다.
"""

from __future__ import annotations

from pathlib import Path

from noticedelta.appspec import AppDefinition, load_app
from noticedelta.impact import keyword_detector
from noticedelta.sources import CATALOG

DEFINITION = Path(__file__).parent / "apps" / "grant-alerts.json"
# 출처와 키, 구조화 값은 출처 카탈로그(sources/catalog/*.json)에 있는 것만 쓸 수 있다
APP: AppDefinition = load_app(
    DEFINITION,
    sources={sid: s.readable_keys() for sid, s in CATALOG.items()},
    meta={sid: set(s.meta) for sid, s in CATALOG.items()},
)

FIELD_TYPES: dict[str, str] = APP.value_types()
FIELD_GUIDE: dict[str, str] = APP.guides()  # LLM에 주는 안내. 바꾸면 녹화본 키가 바뀐다
RULES_FIELD: str = APP.subjects.rules_field or ""
META_FIELDS: tuple[str, ...] = APP.meta_fields()  # 출처가 구조화해 준 값에서 읽는 필드

# 필드별 낱말: 바뀐 조각이 어느 필드와 관련 있을지 고른다(impact.keyword_detector).
# 날짜·금액이 든 조각은 필드 타입으로 따로 고른다.
KEYWORDS: dict[str, str] = APP.keyword_patterns()
candidates = keyword_detector(KEYWORDS, FIELD_TYPES)

# 자격 속성 사전. 단위·배수·금액은 인용 검증이, derive는 시간에 따른 자격이 쓴다.
ATTRIBUTES = APP.rule_attributes()
ATTRIBUTE_LABELS: dict[str, str] = {n: a.label for n, a in APP.subjects.attributes.items()}
RULES_CONTEXT = {"attributes": ATTRIBUTES}  # RuleGroup.model_validate(..., context=RULES_CONTEXT)

# 시·도의 짧은 이름 (region_sido 값)
SIDO: tuple[str, ...] = tuple(APP.subjects.attributes["region_sido"].values)

MASS_IMPACT: int = APP.alerts.hold.mass_impact
MIN_CONFIDENCE: float = APP.alerts.hold.min_confidence
