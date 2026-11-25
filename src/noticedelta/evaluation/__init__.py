"""평가: 기록된 정정 이벤트를 방식별로 다시 돌려 같은 표에 놓는다 (DESIGN 12장, 작업 목록 14·15번).

    dataset   평가 이벤트(사업별 정정의 흐름)와 개발·시험 분리, 사람 라벨 읽기
    methods   B0 메타 비교 · B2 전체 재추출 · B3 diff 기반 LLM · 변경 영향 엔진
    metrics   사람 라벨 기준 재현율·정밀도·가짜 알림률(95% 구간)
    run       방식들을 같은 이벤트에 돌리고 표로 낸다

방식은 추출기와 게이트웨이를 밖에서 받는다. 그래서 하네스를 켠 게이트웨이를 주면
"엔진 + 하네스"가 표의 한 줄이 된다(ARCHITECTURE 3.2의 하네스 실험).
"""

from noticedelta.evaluation.dataset import (
    Chain,
    EvalEvent,
    load_chains,
    load_labels,
    split_of,
)
from noticedelta.evaluation.methods import (
    DIFF_TEMPLATE,
    DIFF_TEMPLATE_V2,
    B0Meta,
    B2Full,
    B3Diff,
    Engine,
    Fields,
    Outcome,
)
from noticedelta.evaluation.metrics import Label, Score, score, wilson
from noticedelta.evaluation.run import (
    Row,
    disagreements,
    evaluate,
    input_sizes,
    render,
    render_by_field,
)

__all__ = [
    "DIFF_TEMPLATE",
    "DIFF_TEMPLATE_V2",
    "B0Meta",
    "B2Full",
    "B3Diff",
    "Chain",
    "Engine",
    "EvalEvent",
    "Label",
    "Outcome",
    "Row",
    "Score",
    "Fields",
    "disagreements",
    "evaluate",
    "input_sizes",
    "load_chains",
    "load_labels",
    "render",
    "render_by_field",
    "score",
    "split_of",
    "wilson",
]
