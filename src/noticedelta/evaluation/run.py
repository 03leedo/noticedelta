"""방식들을 같은 이벤트에 돌리고 표로 낸다."""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from noticedelta.evaluation.dataset import Chain
from noticedelta.evaluation.methods import Method
from noticedelta.evaluation.metrics import Label, Score, score, wilson

TEXT_ONLY = ("body", "attachment")  # API 메타에는 안 보이고 본문·첨부에서만 보이는 변경
_SUMS = ("calls", "input_chars", "input_tokens", "output_tokens", "planned_calls", "planned_tokens")


@dataclass
class Row:
    name: str
    events: int
    judged: int  # 판단한 이벤트 수. 녹화본이 없으면 줄어든다
    calls: int
    input_chars: int
    input_tokens: int
    output_tokens: int
    planned_calls: int  # 미리 세기: 녹화본이 없어 보냈어야 할 호출(처음 추출 포함)
    planned_tokens: int
    predictions: dict[str, set[str] | None]
    score: Score
    score_meta: Score  # API 메타로 보이는 변경만 양성으로
    score_text: Score  # 본문·첨부에서만 보이는 변경만 양성으로
    costs: dict[str, dict[str, int]] = field(default_factory=dict)  # 이벤트마다 _SUMS 값
    notes: dict[str, str] = field(default_factory=dict)  # 판단하지 못한 이벤트의 이유
    # 사업마다 처음 추출의 미리 세기: (그 사업의 이벤트들, {planned_calls, planned_tokens})
    start_costs: list[tuple[tuple[str, ...], dict[str, int]]] = field(default_factory=list)


def evaluate(
    chains: Sequence[Chain],
    methods: Mapping[str, Callable[[], Method]],
    labels: Mapping[tuple[str, str], Label],
) -> list[Row]:
    """methods는 {표에 쓸 이름: 방식을 새로 만드는 함수}. 사업마다 새로 만들어 상태가 섞이지 않게 한다."""
    rows = []
    for name, make in methods.items():
        predictions: dict[str, set[str] | None] = {}
        total = dict.fromkeys(_SUMS, 0)
        costs: dict[str, dict[str, int]] = {}
        notes: dict[str, str] = {}
        start_costs: list[tuple[tuple[str, ...], dict[str, int]]] = []
        for chain in chains:
            method = make()
            method.start(chain.first_segments)
            first = getattr(method, "start_cost", None)
            if first is not None:  # 처음 추출은 이벤트 비용이 아니지만 미리 세기에는 든다
                total["planned_calls"] += first.planned_calls
                total["planned_tokens"] += first.planned_tokens
                start_costs.append(
                    (
                        tuple(ev.event_id for ev in chain.events),
                        {
                            "planned_calls": first.planned_calls,
                            "planned_tokens": first.planned_tokens,
                        },
                    )
                )
            for ev in chain.events:
                out = method.step(ev)
                predictions[ev.event_id] = out.changed
                costs[ev.event_id] = {k: getattr(out, k) for k in _SUMS}
                if out.changed is None:
                    notes[ev.event_id] = out.note
                for k in _SUMS:
                    total[k] += getattr(out, k)
        rows.append(
            Row(
                name=name,
                events=len(predictions),
                judged=sum(p is not None for p in predictions.values()),
                calls=total["calls"],
                input_chars=total["input_chars"],
                input_tokens=total["input_tokens"],
                output_tokens=total["output_tokens"],
                planned_calls=total["planned_calls"],
                planned_tokens=total["planned_tokens"],
                predictions=predictions,
                score=score(predictions, labels),
                score_meta=score(predictions, labels, visible_in=("api_meta",)),
                score_text=score(predictions, labels, visible_in=TEXT_ONLY),
                costs=costs,
                notes=notes,
                start_costs=start_costs,
            )
        )
    return rows


def common_events(rows: Sequence[Row]) -> set[str]:
    """모든 방식이 판단한 이벤트. 방식끼리 같은 정정으로 비교할 때 쓴다(시험 세트 동결 표)."""
    if not rows:
        return set()
    return set.intersection(*({e for e, p in r.predictions.items() if p is not None} for r in rows))


def restrict(
    rows: Sequence[Row], events: set[str], labels: Mapping[tuple[str, str], Label]
) -> list[Row]:
    """이 이벤트들만 남긴 표. 판단·토큰·점수를 그 이벤트들로 다시 센다(다시 돌리지 않는다).

    처음 추출의 미리 세기는 남은 이벤트가 하나라도 있는 사업의 것만 더한다(--plan --common).
    """
    out = []
    for r in rows:
        preds = {e: p for e, p in r.predictions.items() if e in events}
        sums = {k: sum(r.costs.get(e, {}).get(k, 0) for e in preds) for k in _SUMS}
        for ids, cost in r.start_costs:
            if any(e in preds for e in ids):
                for k, v in cost.items():
                    sums[k] += v
        out.append(
            replace(
                r,
                events=len(preds),
                judged=sum(p is not None for p in preds.values()),
                predictions=preds,
                score=score(preds, labels),
                score_meta=score(preds, labels, visible_in=("api_meta",)),
                score_text=score(preds, labels, visible_in=TEXT_ONLY),
                costs={e: c for e, c in r.costs.items() if e in preds},
                notes={e: n for e, n in r.notes.items() if e in preds},
                start_costs=[(ids, c) for ids, c in r.start_costs if any(e in preds for e in ids)],
                **sums,
            )
        )
    return out


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
        "| 방식 | 이벤트 | 판단함 | 변경 있다고 봄 | LLM 호출 | 입력 글자 | 입력 토큰 | 출력 토큰 | 재현율 | 정밀도 | 가짜 알림률 | 재현율: 메타로 보이는 변경 | 재현율: 본문·첨부에서만 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        s = r.score
        said = sum(bool(p) for p in r.predictions.values())
        lines.append(
            f"| {r.name} | {r.events} | {r.judged} | {said} | {r.calls} | {r.input_chars:,} "
            f"| {r.input_tokens:,} | {r.output_tokens:,} "
            f"| {_rate(s.tp, s.tp + s.fn)} | {_rate(s.tp, s.tp + s.fp)} | {_rate(s.fp, s.fp + s.tn)} "
            f"| {_rate(r.score_meta.tp, r.score_meta.tp + r.score_meta.fn)} "
            f"| {_rate(r.score_text.tp, r.score_text.tp + r.score_text.fn)} |"
        )
    if any(r.planned_calls for r in rows):
        lines += [
            "",
            "미리 세기: 녹화본이 없어 실제로 돌리면 보낼 호출 (같은 요청은 한 번만 센다):",
            "",
            "| 방식 | 보낼 호출 | 입력 토큰 어림 |",
            "|---|---|---|",
        ]
        lines += [
            f"| {r.name} | {r.planned_calls} | {r.planned_tokens:,} |"
            for r in rows
            if r.planned_calls
        ]
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


def render_by_field(rows: Sequence[Row], labels: Mapping[tuple[str, str], Label]) -> str:
    """방식×필드로 맞힘·놓침·가짜 알림. 가짜 알림 가운데 '문구만 바뀜'을 바뀌었다고 한 것은 따로 센다."""
    fields = sorted({f for _, f in labels})
    head = "| 방식 | 필드 | 실제 바뀜 | 맞힘 | 놓침 | 가짜 알림 | 그중 문구만 바뀜 |\n|---|---|---|---|---|---|---|"
    lines = [head]
    for row in rows:
        for name in fields:
            tp = fn = fp = fp_wording = 0
            for (event_id, f), label in labels.items():
                said = row.predictions.get(event_id)
                if f != name or said is None:
                    continue
                if label.status == "changed":
                    tp += name in said
                    fn += name not in said
                elif name in said:
                    fp += 1
                    fp_wording += label.status == "wording_only"
            lines.append(f"| {row.name} | {name} | {tp + fn} | {tp} | {fn} | {fp} | {fp_wording} |")
    return "\n".join(lines)


def with_meta(
    base: Row,
    meta: Row,
    fields: Sequence[str],
    labels: Mapping[tuple[str, str], Label],
    name: str | None = None,
) -> Row:
    """출처 값으로 읽는 필드(fields, 예: 마감)는 메타 비교(B0)의 판단을, 나머지는 base의 판단을 쓴다.

    토큰 칸은 base 그대로다(위 한도). 실제로는 바뀐 필드가 출처 값 필드뿐인 정정에서 LLM을 부르지 않는다.
    """
    own = set(fields)
    preds: dict[str, set[str] | None] = {}
    for event_id, said in base.predictions.items():
        m = meta.predictions.get(event_id) or set()
        preds[event_id] = None if said is None else (said - own) | (m & own)
    return Row(
        name=name or f"{base.name} + 출처 값({', '.join(sorted(own))})",
        events=base.events,
        judged=base.judged,
        calls=base.calls,
        input_chars=base.input_chars,
        input_tokens=base.input_tokens,
        output_tokens=base.output_tokens,
        planned_calls=base.planned_calls,
        planned_tokens=base.planned_tokens,
        predictions=preds,
        score=score(preds, labels),
        score_meta=score(preds, labels, visible_in=("api_meta",)),
        score_text=score(preds, labels, visible_in=TEXT_ONLY),
        costs=base.costs,
        notes=base.notes,
        start_costs=base.start_costs,
    )
