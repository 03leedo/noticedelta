"""평가 실행: 기록된 정정을 방식별로 다시 돌려 표를 낸다 (DESIGN 12장, `make eval`).

    make eval                                           # 개발 세트, 녹화본만 (LLM 비용 0)
    uv run python scripts/eval.py --split test          # 시험 세트 (측정 동결 때만 본다)
    uv run python scripts/eval.py --harness personal    # 엔진에 하네스를 켠 줄을 더한다
    uv run python scripts/eval.py --disagreements       # 엔진과 B2가 다르게 본 (이벤트, 필드) 목록
    uv run python scripts/eval.py --mode replay_or_live # 녹화본이 없는 호출은 실제로 부르고 녹화한다

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
    B0Meta,
    B2Full,
    B3Diff,
    Engine,
    disagreements,
    evaluate,
    input_sizes,
    load_chains,
    load_labels,
    render,
)
from noticedelta.llm import AnthropicProvider, FileCassetteStore, Gateway, Mode  # noqa: E402
from noticedelta.llm.extract import LLMExtractor  # noqa: E402
from noticedelta.llm.harness import for_sensitivity  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.REPLAY.value)
    ap.add_argument("--harness", choices=["public", "personal"], help="하네스를 켠 엔진을 더한다")
    ap.add_argument("--disagreements", action="store_true")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    mode = Mode(args.mode)
    store = FileCassetteStore(ROOT / "data" / "cassettes")
    provider = None if mode is Mode.REPLAY else AnthropicProvider()

    def extractor(harnesses=()):
        return LLMExtractor(Gateway(provider, store, mode, harnesses=harnesses))

    plain, diff_gateway = extractor(), Gateway(provider, store, mode)
    methods = {
        "B0 메타 비교": B0Meta,
        "B2 전체 재추출": lambda: B2Full(plain),
        "B3 diff 기반 LLM": lambda: B3Diff(plain, diff_gateway),
        "변경 영향 엔진": lambda: Engine(plain),
    }
    if args.harness:
        guarded = extractor(for_sensitivity(args.harness))
        methods[f"엔진 + 하네스({args.harness})"] = lambda: Engine(guarded)

    with factory() as s:
        chains = load_chains(s, split=None if args.split == "all" else args.split)
        labels = load_labels(s)
    events = sum(len(c.events) for c in chains)
    labeled = len({e for e, _ in labels} & {ev.event_id for c in chains for ev in c.events})
    print(
        f"세트: {args.split} · 사업 {len(chains)}개 · 정정 이벤트 {events}건 · 라벨이 있는 이벤트 {labeled}건\n"
    )
    rows = evaluate(chains, methods, labels)
    print(render(rows, input_sizes(chains)))
    if args.disagreements:
        by_name = {r.name: r for r in rows}
        pairs = disagreements(by_name["변경 영향 엔진"], by_name["B2 전체 재추출"])
        print(f"\n엔진과 B2가 다르게 본 (이벤트, 필드) {len(pairs)}건")
        for event_id, name in pairs:
            print(f"  {event_id} {name}")


if __name__ == "__main__":
    main()
