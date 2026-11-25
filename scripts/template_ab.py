"""추출 틀 비교: 같은 공고에 두 추출기를 돌려 토큰과 값을 비교한다 (docs/research/eval.md).

    # v1(조각 ID) 대 v2(짧은 번호). v1은 녹화본으로, v2는 없으면 부른다
    uv run python scripts/template_ab.py --provider pickle --limit 30 --budget 200000
    # 잡음 바닥: v1을 한 번 더 부른다(다른 녹화본 폴더에 남겨 다음에는 재생된다)
    uv run python scripts/template_ab.py --provider pickle --limit 10 --noise --budget 120000
    # 녹화본만으로 같은 표를 다시 낸다(비용 0)
    uv run python scripts/template_ab.py --mode replay --limit 30

대상은 평가 개발 세트의 사업마다 첫 버전(처음 추출과 같은 입력)이다. 출력은 숫자와 ID뿐이다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.evaluation import load_chains  # noqa: E402
from noticedelta.evaluation.ab import Side, render_ab, run_ab  # noqa: E402
from noticedelta.llm import (  # noqa: E402
    Budget,
    FileCassetteStore,
    Gateway,
    Mode,
    calibrate,
    choose,
    make_provider,
)
from noticedelta.llm.extract import (  # noqa: E402
    FIELD_TYPES,
    TEMPLATE_V1,
    TEMPLATE_V2,
    LLMExtractor,
)
from noticedelta.programs import api_meta  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--limit", type=int, default=30, help="앞에서부터 사업 몇 개")
    ap.add_argument("--noise", action="store_true", help="v1 대 v1 다시 부르기(잡음 바닥)")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.REPLAY_OR_LIVE.value)
    ap.add_argument("--provider", choices=["anthropic", "pickle"])
    ap.add_argument("--model")
    ap.add_argument("--budget", type=int, help="실제로 보낼 토큰(입력+출력)의 상한")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    mode = Mode(args.mode)
    choice = choose(args.provider, args.model)
    provider = None if mode in (Mode.REPLAY, Mode.PLAN) else make_provider(choice)
    budget = Budget(args.budget) if args.budget else None
    main_store = FileCassetteStore(ROOT / "data" / "cassettes")
    # 잡음 바닥의 두 번째 호출은 같은 요청이라 키가 같다. 다른 폴더에 남겨야 새로 부른다
    repeat_store = FileCassetteStore(ROOT / "data" / "cassettes-repeat")

    def side(template: str, store: FileCassetteStore) -> Side:
        ratio = calibrate(main_store.records(), choice.model, template) or 0.7
        gw = Gateway(provider, store, mode, budget=budget, tokens_per_char=ratio)
        return Side(LLMExtractor(gw, model=choice.model, template=template), gw)

    a = side(TEMPLATE_V1, main_store)
    if args.noise:
        name, b = "v1 대 v1 다시(잡음 바닥)", side(TEMPLATE_V1, repeat_store)
    else:
        name, b = "v1 대 v2(짧은 번호)", side(TEMPLATE_V2, main_store)

    with factory() as s:
        chains = load_chains(s, split=None if args.split == "all" else args.split, meta=api_meta)
    docs = [(c.item_id, c.first_segments) for c in chains if c.first_segments][: args.limit]
    print(f"모델: {choice.name}/{choice.model} · 세트: {args.split} · 문서 {len(docs)}개\n")
    res = run_ab(docs, FIELD_TYPES, a, b)
    print(render_ab([(name, res)]))
    if res.differing:
        print(f"\n값이 다른 (문서, 필드) {len(res.differing)}건")
        for doc, field, outcome in res.differing:
            print(f"  {doc} {field} {outcome}")
    live = a.gateway.usage.live_tokens + b.gateway.usage.live_tokens
    print(f"\n이번에 실제로 쓴 토큰(입력+출력): {live:,}")


if __name__ == "__main__":
    main()
