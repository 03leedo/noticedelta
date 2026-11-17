"""사람 라벨 기준 지표 (DESIGN 3장 RQ1). 단위는 (이벤트, 필드) 하나."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass

POSITIVE = "changed"  # 그 밖(unchanged, wording_only, absent)은 "알리면 안 되는 것"


@dataclass(frozen=True)
class Label:
    status: str  # changed / unchanged / wording_only / absent
    visible_in: str | None = None  # api_meta / body / attachment


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """비율 k/n의 95% 구간 (Wilson). 표본이 작아도 0~1을 벗어나지 않는다."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class Score:
    tp: int = 0  # 바뀐 것을 바뀌었다고 함
    fn: int = 0  # 바뀐 것을 놓침
    fp: int = 0  # 안 바뀐 것(문구만 바뀜 포함)을 바뀌었다고 함: 가짜 알림
    tn: int = 0

    @property
    def labeled(self) -> int:
        return self.tp + self.fn + self.fp + self.tn

    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    def false_alert_rate(self) -> float | None:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else None


def score(
    predictions: Mapping[str, set[str] | None],
    labels: Mapping[tuple[str, str], Label],
    *,
    visible_in: Collection[str] | None = None,
) -> Score:
    """predictions: {이벤트 ID: 바뀌었다고 본 필드들}. None(판단하지 못한 이벤트)은 세지 않는다.

    visible_in을 주면 그 가운데 한 곳에서 보이는 변경만 양성으로 센다(API 메타로 보이는 것 /
    본문·첨부에서만 보이는 것을 나눠 보려고). 음성은 나누지 않는다.
    """
    s = Score()
    for (event_id, field_name), label in labels.items():
        predicted = predictions.get(event_id)
        if predicted is None:
            continue
        said = field_name in predicted
        if label.status == POSITIVE:
            if visible_in is not None and label.visible_in not in visible_in:
                continue
            if said:
                s.tp += 1
            else:
                s.fn += 1
        elif said:
            s.fp += 1
        else:
            s.tn += 1
    return s
