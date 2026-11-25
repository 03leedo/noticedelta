"""추출기 두 개를 같은 문서에 돌려 토큰과 값을 비교한다 (토큰을 줄인 틀이 답을 바꾸지 않는지).

같은 추출기를 두 번 돌린 불일치(잡음 바닥, DESIGN 3장 RQ1)와 함께 봐야 한다. 두 틀 사이의
불일치가 잡음 바닥과 비슷하면, 틀을 바꿔서 답이 달라졌다기보다 모델이 원래 그만큼 흔들린다고 본다.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from noticedelta.impact import FieldValue
from noticedelta.llm.gateway import BudgetExceeded, Gateway, MissingCassette
from noticedelta.llm.openai_compat import ProviderError
from noticedelta.segments import Segment
from noticedelta.values import values_equal

Extract = Callable[[set[str], Sequence[Segment]], dict[str, FieldValue]]
OUTCOMES = ("same", "differ", "only_a", "only_b", "both_absent")


@dataclass
class Side:
    extract: Extract
    gateway: Gateway


@dataclass
class ABResult:
    docs: int = 0  # 두 쪽 모두 답을 받은 문서
    failed: int = 0  # 어느 한쪽이라도 답을 못 받은 문서(시간 초과, 한도, 예산)
    a_in: list[int] = field(default_factory=list)  # 문서마다 입력 토큰
    b_in: list[int] = field(default_factory=list)
    a_out: list[int] = field(default_factory=list)
    b_out: list[int] = field(default_factory=list)
    fields: dict[str, int] = field(default_factory=lambda: dict.fromkeys(OUTCOMES, 0))
    differing: list[tuple[str, str, str]] = field(default_factory=list)  # (문서, 필드, 결과)

    def agreement(self) -> float | None:
        """값이 있는 필드 가운데 두 쪽이 같은 비율(둘 다 없는 필드는 빼고 센다)."""
        n = sum(v for k, v in self.fields.items() if k != "both_absent")
        return self.fields["same"] / n if n else None


def compare_fields(
    types: Mapping[str, str], a: Mapping[str, FieldValue], b: Mapping[str, FieldValue]
) -> dict[str, str]:
    """필드마다 same / differ / only_a / only_b / both_absent. 비교는 값 타입별(values_equal)."""
    out = {}
    for name, vtype in types.items():
        x, y = a.get(name), b.get(name)
        if x is None and y is None:
            out[name] = "both_absent"
        elif y is None:
            out[name] = "only_a"
        elif x is None:
            out[name] = "only_b"
        else:
            out[name] = "same" if values_equal(vtype, x.value, y.value) else "differ"
    return out


def _call(side: Side, fields: set[str], segments: Sequence[Segment]):
    u = side.gateway.usage
    before = (u.input_tokens, u.output_tokens)
    values = side.extract(fields, segments)
    return values, u.input_tokens - before[0], u.output_tokens - before[1]


def run_ab(
    docs: Sequence[tuple[str, Sequence[Segment]]],
    types: Mapping[str, str],
    a: Side,
    b: Side,
) -> ABResult:
    res = ABResult()
    fields = set(types)
    for doc_id, segments in docs:
        try:
            va, ia, oa = _call(a, fields, segments)
            vb, ib, ob = _call(b, fields, segments)
        except (MissingCassette, ProviderError, BudgetExceeded):
            res.failed += 1
            continue
        res.docs += 1
        res.a_in.append(ia)
        res.b_in.append(ib)
        res.a_out.append(oa)
        res.b_out.append(ob)
        for name, outcome in compare_fields(types, va, vb).items():
            res.fields[outcome] += 1
            if outcome not in ("same", "both_absent"):
                res.differing.append((doc_id, name, outcome))
    return res


def render_ab(rows: Sequence[tuple[str, ABResult]]) -> str:
    lines = [
        "| 비교 | 문서 | 입력 토큰 A | 입력 토큰 B | B/A 합계 | B/A 중앙값 | 출력 토큰 A | 출력 토큰 B "
        "| 값 같음 | 다름 | A에만 | B에만 | 둘 다 없음 | 일치율 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in rows:
        ratios = [b / a for a, b in zip(r.a_in, r.b_in, strict=True) if a]
        agree = r.agreement()
        f = r.fields
        lines.append(
            f"| {name} | {r.docs} (실패 {r.failed}) | {sum(r.a_in):,} | {sum(r.b_in):,} "
            f"| {sum(r.b_in) / max(sum(r.a_in), 1):.1%} "
            f"| {statistics.median(ratios) if ratios else 0:.1%} "
            f"| {sum(r.a_out):,} | {sum(r.b_out):,} "
            f"| {f['same']} | {f['differ']} | {f['only_a']} | {f['only_b']} | {f['both_absent']} "
            f"| {'-' if agree is None else f'{agree:.1%}'} |"
        )
    return "\n".join(lines)
