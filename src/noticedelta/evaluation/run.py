"""방식들을 같은 이벤트에 돌리고 표로 낸다."""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from noticedelta.evaluation.dataset import Chain
from noticedelta.evaluation.methods import Method
from noticedelta.evaluation.metrics import Label, Score, score, wilson

TEXT_ONLY = ("body", "attachment")  # API 메타에는 안 보이고 본문·첨부에서만 보이는 변경


@dataclass
class Row:
    name: str
    events: int
    judged: int  # 판단한 이벤트 수. 녹화본이 없으면 줄어든다
    calls: int
    input_chars: int
    predictions: dict[str, set[str] | None]
    score: Score
    score_meta: Score  # API 메타로 보이는 변경만 양성으로
    score_text: Score  # 본문·첨부에서만 보이는 변경만 양성으로


def evaluate(
    chains: Sequence[Chain],
    methods: Mapping[str, Callable[[], Method]],
    labels: Mapping[tuple[str, str], Label],
) -> list[Row]:
    """methods는 {표에 쓸 이름: 방식을 새로 만드는 함수}. 사업마다 새로 만들어 상태가 섞이지 않게 한다."""
    rows = []
    for name, make in methods.items():
        predictions: dict[str, set[str] | None] = {}
        calls = chars = 0
        for chain in chains:
            method = make()
            method.start(chain.first_segments)
            for ev in chain.events:
                out = method.step(ev)
                predictions[ev.event_id] = out.changed
                calls += out.calls
                chars += out.input_chars
        rows.append(
            Row(
                name=name,
                events=len(predictions),
                judged=sum(p is not None for p in predictions.values()),
                calls=calls,
                input_chars=chars,
                predictions=predictions,
                score=score(predictions, labels),
                score_meta=score(predictions, labels, visible_in=("api_meta",)),
                score_text=score(predictions, labels, visible_in=TEXT_ONLY),
            )
        )
    return rows


def disagreements(a: Row, b: Row) -> list[tuple[str, str]]:
    """두 방식이 다르게 본 (이벤트, 필드). 정답은 아니고, 사람이 볼 목록이다(안전망, DESIGN 7.7)."""
    out = []
    for event_id, pa in a.predictions.items():
        pb = b.predictions.get(event_id)
        if pa is None or pb is None:
            continue
        out += [(event_id, f) for f in sorted(pa ^ pb)]
    return out


def input_sizes(chains: Sequence[Chain]) -> list[dict[str, int]]:
    """LLM을 부르지 않고 잴 수 있는 것: 방식별로 이벤트마다 LLM에 넘길 조각 글자 수.

    엔진은 하한이다. 실제로는 영향받은 필드의 기존 근거 조각이 더 붙는다(추출 결과가 있어야 안다).
    """
    out = []
    for chain in chains:
        for ev in chain.events:
            old = {s.lineage_id: len(s.text) for s in ev.old_segments}
            new = {s.lineage_id: len(s.text) for s in ev.change.segments}
            touched = [*ev.change.added, *ev.change.modified]
            out.append(
                {
                    "b2": sum(new.values()),
                    "b3": sum(new[i] for i in touched)
                    + sum(old.get(i, 0) for i in [*ev.change.modified, *ev.change.removed]),
                    "engine_min": sum(new[i] for i in touched),
                }
            )
    return out


def _rate(k: int, n: int) -> str:
    if n == 0:
        return "라벨 없음"
    lo, hi = wilson(k, n)
    return f"{k / n:.2f} ({lo:.2f}~{hi:.2f}, {k}/{n})"


def render(rows: Sequence[Row], sizes: Sequence[Mapping[str, int]] = ()) -> str:
    """마크다운 표. 사람 라벨이 없는 칸은 "라벨 없음", 판단하지 못한 방식은 판단 수가 0이다."""
    lines = [
        "| 방식 | 이벤트 | 판단함 | 변경 있다고 봄 | LLM 호출 | 입력 글자 | 재현율 | 정밀도 | 가짜 알림률 | 재현율: 메타로 보이는 변경 | 재현율: 본문·첨부에서만 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        s = r.score
        said = sum(bool(p) for p in r.predictions.values())
        lines.append(
            f"| {r.name} | {r.events} | {r.judged} | {said} | {r.calls} | {r.input_chars:,} "
            f"| {_rate(s.tp, s.tp + s.fn)} | {_rate(s.tp, s.tp + s.fp)} | {_rate(s.fp, s.fp + s.tn)} "
            f"| {_rate(r.score_meta.tp, r.score_meta.tp + r.score_meta.fn)} "
            f"| {_rate(r.score_text.tp, r.score_text.tp + r.score_text.fn)} |"
        )
    if sizes:
        b2 = [x["b2"] for x in sizes]
        lines += [
            "",
            "LLM 없이 잰 입력 크기 (이벤트마다 넘길 조각 글자 수):",
            "",
            "| 방식 | 합계 | 이벤트 중앙값 | B2 대비 합계 | B2 대비 중앙값(이벤트별 비율) |",
            "|---|---|---|---|---|",
        ]
        for key, name in (
            ("b2", "B2 전체 재추출"),
            ("b3", "B3 바뀐 조각 쌍"),
            ("engine_min", "엔진 (하한: 바뀐 조각만)"),
        ):
            xs = [x[key] for x in sizes]
            ratios = [x[key] / x["b2"] for x in sizes if x["b2"]]
            lines.append(
                f"| {name} | {sum(xs):,} | {statistics.median(xs):,.0f} "
                f"| {sum(xs) / max(sum(b2), 1):.1%} | {statistics.median(ratios):.1%} |"
            )
    return "\n".join(lines)
