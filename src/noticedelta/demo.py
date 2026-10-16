"""가상 정정공고 시나리오 데모 (설계 4.2, 10.3).

녹화된 LLM 응답(demo/cassettes)으로 재생하므로 API 키·네트워크 없이 돈다.

    uv run python -m noticedelta.demo            # 재생
    uv run python -m noticedelta.demo --record-fake   # 손으로 쓴 가상 응답으로 녹화본 다시 만들기
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from noticedelta.eligibility import RuleGroup, judge
from noticedelta.impact import Alert, Subscription, run_impact
from noticedelta.llm import FileCassetteStore, Gateway, LLMRequest, LLMResponse, Mode
from noticedelta.llm.extract import FIELD_TYPES, LLMExtractor
from noticedelta.matching import match_segments
from noticedelta.segments import Segment, Table, segment_document

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
LABEL = {"eligible": "해당", "ineligible": "비해당", "unknown": "확인 필요"}


@dataclass
class DemoResult:
    alerts: list[Alert]
    affected_fields: set[str]
    real_changes: set[str]
    impact_llm_calls: int
    baseline_llm_calls: int
    live_calls: int


class FakeAnswerProvider:
    """녹화본 생성용. 시나리오에 손으로 적어 둔 가상 응답을 돌려준다."""

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers

    def complete(self, req: LLMRequest) -> LLMResponse:
        lines = dict(re.findall(r"^\[(\S+)\] (.*)$", req.input, flags=re.MULTILINE))
        version = "V2" if any("5년 이내" in t for t in lines.values()) else "V1"
        wanted = req.output_schema["properties"]["fields"]["items"]["properties"]["name"]["enum"]
        out = []
        for name in wanted:
            spec = self.answers[version].get(name)
            if spec is None:
                continue
            sources = [
                lid for lid, t in lines.items() if any(m in t for m in spec["source_contains"])
            ]
            if sources:
                out.append(
                    {
                        "name": name,
                        "value_json": json.dumps(spec["value"], ensure_ascii=False),
                        "sources": sources,
                    }
                )
        return LLMResponse(
            {"fields": out}, input_tokens=len(req.input), output_tokens=50, cost_usd=0.0
        )


def _blocks(raw: list) -> list[str | Table]:
    return [Table(b["table"]) if isinstance(b, dict) else b for b in raw]


def _show(seg: Segment) -> str:
    return seg.text if len(seg.text) <= 40 else seg.text[:40] + "…"


def run_demo(
    *, mode: Mode = Mode.REPLAY, demo_dir: Path = DEMO_DIR, out: Callable[[str], None] = print
) -> DemoResult:
    sc = json.loads((demo_dir / "scenario.json").read_text(encoding="utf-8"))
    provider = FakeAnswerProvider(sc["fake_llm_answers"]) if mode is Mode.RECORD else None
    gateway = Gateway(provider, FileCassetteStore(demo_dir / "cassettes"), mode)
    extractor = LLMExtractor(gateway)
    counter = itertools.count(1)
    new_id = lambda: f"L{next(counter)}"  # noqa: E731

    v1, v2 = sc["versions"]
    users = sc["users"]
    profiles = {u: d["profile"] for u, d in users.items()}
    subs = [Subscription(u, set(d["fields"])) for u, d in users.items()]

    out(f"== {sc['title']} ==")
    how = "가상 응답으로 녹화" if mode is Mode.RECORD else "녹화된 LLM 응답으로 재생"
    out(f"(가상 시나리오, {how})")

    old = match_segments([], segment_document(_blocks(v1["blocks"]), source="v1"), new_id=new_id)
    fields = extractor(set(FIELD_TYPES), old.segments)
    rules = RuleGroup.model_validate(fields["eligibility_rules"].value)
    judgments = {u: judge(rules, p) for u, p in profiles.items()}
    out(f"\n[원공고] 조각 {len(old.segments)}개, 필드 {len(fields)}개 추출")
    for u, j in judgments.items():
        out(f"  사용자 {u}: {LABEL[j.result]}")

    calls_before = gateway.usage.calls
    change = match_segments(
        old.segments, segment_document(_blocks(v2["blocks"]), source="v2"), new_id=new_id
    )
    by_id = {s.lineage_id: s for s in change.segments}
    out(
        f"\n[정정공고] 바뀐 조각 {len(change.modified)}개, 추가 {len(change.added)}개, 삭제 {len(change.removed)}개"
    )
    for lid in change.modified:
        out(f"  수정 {lid}: {_show(by_id[lid])}")

    res = run_impact(
        notice_id=sc["notice_id"],
        to_version_id=v2["version_id"],
        change=change,
        old_fields=fields,
        extractor=extractor,
        subscriptions=subs,
        profiles=profiles,
        old_judgments=judgments,
    )
    impact_calls = gateway.usage.calls - calls_before
    baseline = len(users) * len(FIELD_TYPES)

    out(f"\n[영향 분석] 영향 필드: {', '.join(sorted(res.affected_fields))}")
    out(f"  진짜 바뀐 필드: {', '.join(sorted(res.real_changes)) or '없음'}")
    for name in sorted(res.affected_fields - res.real_changes):
        out(f"  {name}: 문구만 바뀜, 알림 없음")
    out(f"  LLM 호출: {impact_calls}회 (질문별 재실행 방식이라면 {baseline}회)")

    out("\n[알림]")
    for a in res.alerts:
        if a.kind == "judgment_changed":
            out(
                f"  사용자 {a.user_id}: 자격 {LABEL[a.before]} → {LABEL[a.after]} (근거 {', '.join(a.sources)})"
            )
        else:
            out(f"  사용자 {a.user_id}: {a.field} {a.before} → {a.after}")
    out("\n참고용 정보입니다. 최종 확인은 공고 원문으로 하세요.")
    return DemoResult(
        res.alerts,
        res.affected_fields,
        res.real_changes,
        impact_calls,
        baseline,
        gateway.usage.live_calls,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="NoticeDelta 가상 정정공고 데모")
    ap.add_argument("--record-fake", action="store_true", help="가상 응답으로 녹화본을 다시 만든다")
    args = ap.parse_args()
    run_demo(mode=Mode.RECORD if args.record_fake else Mode.REPLAY)


if __name__ == "__main__":
    main()
