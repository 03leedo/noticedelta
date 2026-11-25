"""미리 돌려 보기와 판 비교 (app-spec 10.1·10.2). 알림은 보내지 않고 목록으로 낸다.

    uv run python scripts/backtest.py                                   # 켠 판으로 개발 세트를 다시 흘린다
    uv run python scripts/backtest.py --alert-fields deadline,amount,required_documents,contact
                                                                        # 문의처 변경도 알리는 초안과 비교
    uv run python scripts/backtest.py --draft path/to/app.json          # 앱 정의 파일로 만든 초안과 비교
    uv run python scripts/backtest.py --plan                            # 녹화본이 없는 정정의 비용만 센다

필드는 녹화본으로 뽑는다(LLM 비용 0). 녹화본이 없는 사업은 "녹화본 없음"으로 센다.
구독자는 가상 프로필 N명이 모든 사업의 모든 필드를 구독한 것으로 본다. 무엇을 알릴지는 앱 정의가 정한다.
시험은 사람 라벨이다(--labeler, --override): 바뀐 필드를 알리면 맞는 알림이다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.appspec import load_app  # noqa: E402
from noticedelta.backtest import (  # noqa: E402
    Backtest,
    backtest,
    by_change,
    compare,
    correct,
    draft_with_alert_fields,
    same_extraction,
)
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.db.models import Item  # noqa: E402
from noticedelta.eligibility.synthetic import virtual_profiles  # noqa: E402
from noticedelta.evaluation import load_chains, load_labels  # noqa: E402
from noticedelta.grants import APP  # noqa: E402
from noticedelta.llm import FileCassetteStore, Gateway, Mode, choose  # noqa: E402
from noticedelta.llm.extract import TEMPLATE_V1, LLMExtractor, spec_for  # noqa: E402
from noticedelta.names import FIELD_NAMES  # noqa: E402
from noticedelta.programs import api_meta  # noqa: E402

KIND = {"field_changed": "값 변경", "judgment_changed": "판정 변경"}


def _summary(rows: list[tuple[str, Backtest]]) -> str:
    keys = list(rows[0][1].summary())
    head = "| | " + " | ".join(name for name, _ in rows) + " |\n|---|" + "---|" * len(rows)
    lines = [head]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(r.summary()[k]) for _, r in rows) + " |")
    for k, get in [
        ("돌린 정정", lambda r: f"{r.judged}/{r.events}"),
        ("녹화본 없음", lambda r: str(len(r.missing))),
        ("LLM 호출(녹화본 포함)", lambda r: str(r.calls)),
        (
            "보내야 할 호출(미리 세기)",
            lambda r: f"{r.planned_calls} (입력 토큰 어림 {r.planned_tokens:,})",
        ),
    ]:
        lines.append(f"| {k} | " + " | ".join(get(r) for _, r in rows) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--subscribers", type=int, default=20, help="가상 구독자 수")
    ap.add_argument("--draft", type=Path, help="비교할 초안 앱 정의(JSON)")
    ap.add_argument("--alert-fields", help="값 변경을 알릴 필드만 바꾼 초안(쉼표로)")
    ap.add_argument(
        "--plan", action="store_true", help="녹화본이 없는 호출은 보내지 않고 비용만 센다"
    )
    ap.add_argument("--provider", choices=["anthropic", "pickle"], default="pickle")
    ap.add_argument("--model", default="pickle-general")
    ap.add_argument("--labeler", default="claude")
    ap.add_argument("--override", default="me")
    ap.add_argument("--show", type=int, default=15)
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    choice = choose(args.provider, args.model)
    mode = Mode.PLAN if args.plan else Mode.REPLAY
    gateway = Gateway(None, FileCassetteStore(ROOT / "data" / "cassettes"), mode)

    def extractor(app):
        # 앱마다 자기 필드 정의로 추출한다. 필드 정의가 같으면 요청이 같아 녹화본을 함께 쓴다
        return LLMExtractor(gateway, model=choice.model, template=TEMPLATE_V1, spec=spec_for(app))

    profiles = {f"v{i:03d}": p for i, p in enumerate(virtual_profiles(args.subscribers))}

    with factory() as s:
        chains = load_chains(s, split=None if args.split == "all" else args.split, meta=api_meta)
        labels = load_labels(s, labeler=args.labeler)
        if args.override:
            human = load_labels(s, labeler=args.override)
            labels |= {k: v for k, v in human.items() if k in labels}
        titles = dict(s.execute(sa.select(Item.id, Item.title)).all())
    events = {ev.event_id: c.item_id for c in chains for ev in c.events}

    def title(event_id: str) -> str:
        return (titles.get(events.get(event_id, ""), "") or event_id)[:40]

    usage = lambda: gateway.usage  # noqa: E731
    on = backtest(APP, chains, extractor(APP), profiles, usage=usage)
    rows = [(f"켠 판 {on.app}", on)]
    draft = None
    if args.draft:
        draft = load_app(args.draft)
    elif args.alert_fields:
        draft = draft_with_alert_fields(APP, [f for f in args.alert_fields.split(",") if f])
    if draft is not None:
        if not same_extraction(APP, draft, spec_for) and not args.plan:
            print(
                "초안의 필드 정의가 달라 추출 요청이 바뀝니다. 녹화본이 없는 사업은 "
                "'녹화본 없음'으로 셉니다. --plan으로 비용을 보세요.\n"
            )
        d = backtest(draft, chains, extractor(draft), profiles, usage=usage)
        rows.append((f"초안 {d.app}", d))

    print(
        f"세트 {args.split} · 정정 {on.events}건 · 가상 구독자 {len(profiles)}명 · 시험(라벨) {len(labels)}칸\n"
    )
    print(_summary(rows))
    if args.plan:
        # 미리 세기에서는 녹화본이 없는 호출이 빈 값으로 돌아와 알림이 맞지 않는다. 비용만 본다
        print(
            "\n미리 세기: 위의 '보내야 할 호출'이 비용입니다. 알림 비교는 녹화본을 채운 뒤에 하세요."
        )
        return

    def listing(cards, mark=None):
        out = []
        for (eid, kind, fname), n in by_change(cards)[: args.show]:
            ok = correct(
                next(c for c in cards if (c.event_id, c.kind, c.field) == (eid, kind, fname)),
                labels,
            )
            tag = "" if ok is None else (" · 라벨: 맞음" if ok else " · 라벨: 가짜")
            out.append(
                f"  - {title(eid)} · {KIND[kind]} · {FIELD_NAMES.get(fname, fname)} · {n}명{tag}"
            )
        return out

    if draft is None:
        print("\n알림 목록 (정정, 종류, 필드, 받는 사람 수):")
        print("\n".join(listing(on.cards)))
        return
    diff = compare(on, rows[1][1], labels)
    print(
        f"\n비교: 생긴 알림 {len(diff.new)}건(맞음 {len(diff.right_new)}, 가짜 {len(diff.fake_new)}) · "
        f"사라진 알림 {len(diff.gone)}건(깨진 시험 {len(diff.broken)}) · 내용이 바뀐 알림 {len(diff.changed)}건"
    )
    if diff.new:
        print("\n생긴 알림:")
        print("\n".join(listing(diff.new)))
    if diff.gone:
        print("\n사라진 알림:")
        print("\n".join(listing(diff.gone)))


if __name__ == "__main__":
    main()
