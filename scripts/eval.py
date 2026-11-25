"""평가 실행: 기록된 정정을 방식별로 다시 돌려 표를 낸다 (DESIGN 12장, `make eval`).

    make eval                                           # 개발 세트, 녹화본만 (LLM 비용 0)
    uv run python scripts/eval.py --split test          # 시험 세트 (측정 동결 때만 본다)
    uv run python scripts/eval.py --harness personal    # 엔진에 하네스를 켠 줄을 더한다
    uv run python scripts/eval.py --disagreements       # 엔진과 B2가 다르게 본 (이벤트, 필드) 목록
    uv run python scripts/eval.py --focus-out data/labels/focus-dev.json
                                                        # 라벨 화면 순서 파일 (갈린 정정 먼저, 값은 안 씀)
    uv run python scripts/eval.py --mode replay_or_live # 녹화본이 없는 호출은 실제로 부르고 녹화한다
    uv run python scripts/eval.py --provider pickle --mode replay_or_live  # 피클 학교 모델(무료)
    uv run python scripts/eval.py --provider pickle --plan         # 보내지 않고 토큰이 얼마나 들지 센다
    uv run python scripts/eval.py --provider pickle --mode replay_or_live --budget 300000
                                                        # 실제로 보낼 토큰(입력+출력)의 상한

녹화본은 data/cassettes에 둔다(공고 원문이 들어 있어 저장소에 올리지 않는다).
녹화본이 없는 방식은 "판단함"이 0으로 나온다. 사람 라벨이 없으면 정확도 칸은 "라벨 없음"이다.
출력은 숫자와 ID뿐이다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.evaluation import (  # noqa: E402
    DIFF_TEMPLATE,
    DIFF_TEMPLATE_V2,
    B0Meta,
    B2Full,
    B3Diff,
    Engine,
    Fields,
    disagreements,
    evaluate,
    input_sizes,
    load_chains,
    load_labels,
    render,
    render_by_field,
)
from noticedelta.grants import KEYWORDS, META_FIELDS  # noqa: E402
from noticedelta.labeling.store import write_focus  # noqa: E402
from noticedelta.llm import (  # noqa: E402
    TOKENS_PER_CHAR,
    Budget,
    FileCassetteStore,
    Gateway,
    Mode,
    calibrate,
    choose,
    make_provider,
)
from noticedelta.llm.extract import (  # noqa: E402
    DIFF_SYSTEM,
    FIELD_GUIDE,
    FIELD_TYPES,
    TEMPLATE_V1,
    TEMPLATE_V2,
    LLMExtractor,
)
from noticedelta.llm.harness import for_sensitivity  # noqa: E402
from noticedelta.programs import api_meta  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.REPLAY.value)
    ap.add_argument("--harness", choices=["public", "personal"], help="하네스를 켠 엔진을 더한다")
    ap.add_argument("--disagreements", action="store_true")
    ap.add_argument("--labeler", default="me", help="이 이름으로 단 라벨로 채점한다 (예: claude)")
    ap.add_argument("--focus-out", type=Path, help="라벨 화면 순서 파일을 쓴다 (label.py --focus)")
    ap.add_argument("--provider", choices=["anthropic", "pickle"], help="기본은 .env의 공급자")
    ap.add_argument("--model", help="기본은 공급자의 기본 모델(.env의 PICKLE_MODEL 등)")
    ap.add_argument(
        "--template",
        choices=[TEMPLATE_V1, TEMPLATE_V2],
        default=TEMPLATE_V1,
        help="추출 틀. 개발 세트의 녹화본은 v1이다. v2는 조각 ID 대신 짧은 번호(입력 토큰 절감)",
    )
    ap.add_argument(
        "--plan", action="store_true", help="보내지 않고 녹화본에 없는 호출의 크기만 센다"
    )
    ap.add_argument(
        "--budget", type=int, help="실제로 보낼 토큰(입력+출력)의 상한. 넘는 호출은 보내지 않는다"
    )
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    mode = Mode.PLAN if args.plan else Mode(args.mode)
    store = FileCassetteStore(ROOT / "data" / "cassettes")
    choice = choose(args.provider, args.model)
    provider = None if mode in (Mode.REPLAY, Mode.PLAN) else make_provider(choice)
    budget = Budget(args.budget) if args.budget else None
    # 토큰 어림 비율: 이 모델로 녹화한 추출 호출에서 잰 값, 없으면 기본값
    template = args.template
    diff_template = DIFF_TEMPLATE if template == TEMPLATE_V1 else DIFF_TEMPLATE_V2
    ratio = calibrate(store.records(), choice.model, template) or TOKENS_PER_CHAR

    def gateway(harnesses=()):
        return Gateway(
            provider, store, mode, harnesses=harnesses, budget=budget, tokens_per_char=ratio
        )

    def extractor(harnesses=()):
        return LLMExtractor(gateway(harnesses), model=choice.model, template=template)

    # 지원사업 팩의 필드 정의. API가 구조화해 주는 것은 접수 기간뿐이다
    spec = Fields(
        FIELD_TYPES, FIELD_GUIDE, meta=META_FIELDS, diff_system=DIFF_SYSTEM, keywords=KEYWORDS
    )
    plain, diff_gateway = extractor(), gateway()
    methods = {
        "B0 메타 비교": lambda: B0Meta(spec),
        "B2 전체 재추출": lambda: B2Full(plain, spec),
        "B3 diff 기반 LLM": lambda: B3Diff(
            plain, diff_gateway, spec, model=choice.model, template=diff_template
        ),
        "변경 영향 엔진": lambda: Engine(plain, spec),
    }
    if args.harness:
        guarded = extractor(for_sensitivity(args.harness))
        methods[f"엔진 + 하네스({args.harness})"] = lambda: Engine(guarded, spec)

    with factory() as s:
        chains = load_chains(s, split=None if args.split == "all" else args.split, meta=api_meta)
        labels = load_labels(s, labeler=args.labeler)
    events = sum(len(c.events) for c in chains)
    labeled = len({e for e, _ in labels} & {ev.event_id for c in chains for ev in c.events})
    print(
        f"모델: {choice.name}/{choice.model} · 틀: {template} · 세트: {args.split} · 사업 {len(chains)}개 · 정정 이벤트 {events}건 · 라벨이 있는 이벤트 {labeled}건\n"
    )
    rows = evaluate(chains, methods, labels)
    print(render(rows, input_sizes(chains)))
    if labeled:
        print(f"\n필드별 (라벨: {args.labeler}):\n")
        print(render_by_field(rows, labels))
    if mode is Mode.PLAN:
        n, t = sum(r.planned_calls for r in rows), sum(r.planned_tokens for r in rows)
        print(f"\n미리 세기 합계: 보낼 호출 {n}개, 입력 토큰 어림 {t:,} (토큰/글자 {ratio:.3f})")
    if budget is not None:
        print(f"\n토큰 예산: {budget.limit:,} 중 {budget.spent:,} 씀")
    by_name = {r.name: r for r in rows}
    pairs = disagreements(by_name["변경 영향 엔진"], by_name["B2 전체 재추출"])
    if args.disagreements:
        print(f"\n엔진과 B2가 다르게 본 (이벤트, 필드) {len(pairs)}건")
        for event_id, name in pairs:
            print(f"  {event_id} {name}")
    if args.focus_out:
        split = {e for e, _ in pairs}
        order = [ev.event_id for c in chains for ev in c.events]
        focus = [(e, "first") for e in order if e in split]
        focus += [(e, "rest") for e in order if e not in split]
        write_focus(args.focus_out, focus, split=args.split)
        first = len(split)
        print(f"\n라벨 순서 파일: {args.focus_out} (먼저 {first}건, 그다음 {len(order) - first}건)")


if __name__ == "__main__":
    main()
