"""화면·알림·캘린더·라벨에 쓰는 한국어 이름. 앱 정의(apps/grant-alerts.json)의 labels에서 읽는다."""

from __future__ import annotations

from noticedelta.grants import APP

RESULT_NAMES: dict[str, str] = dict(APP.labels.results)

FIELD_NAMES: dict[str, str] = dict(APP.labels.fields)

# 알림 본문에 쓰는 표시 이름 (notify.render의 labels)
ALERT_LABELS = {**FIELD_NAMES, **RESULT_NAMES}
