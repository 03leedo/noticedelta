"""미리 돌려 보기와 판 비교 (app-spec 10.1·10.2): 기록된 정정을 앱 정의로 다시 흘려 알림 목록을 낸다.

    result = backtest(app, chains, extractor, profiles)       # 알림은 보내지 않고 목록으로
    diff = compare(켠 판의 결과, 초안의 결과, labels)          # 생긴·사라진·바뀐 알림, 깨진 시험

- 정정은 평가 틀과 같은 것을 쓴다(evaluation.load_chains: DB의 변경 기록을 다시 흘린 사업별 흐름).
- 필드는 추출기로 뽑는다. 녹화본 재생 게이트웨이를 주면 비용 0이고, 녹화본이 없는 사업은
  "녹화본 없음"으로 센다. PLAN 게이트웨이를 주면 보내지 않고 비용만 센다.
- 구독자는 가상 프로필 N명이 모든 사업의 모든 필드를 구독한 것으로 본다. 무엇을 알릴지는 앱 정의의
  alerts.rules가 정한다(영향 계산이 alert_fields·verdict_alerts로 따른다).
- 붙잡기: 근거 신뢰도가 hold.min_confidence보다 낮으면 "확인 필요", 한 정정이 hold.mass_impact명보다
  많은 사람을 비해당으로 바꾸면 그 알림들은 "붙잡힘".
- 시험: 사람 라벨(label 표)이 곧 앱의 시험이다(10.3). 바뀐 필드를 알린 알림은 맞는 알림이다. 맞던
  알림이 초안에서 사라지면 깨진 시험이고, 초안에서 새로 생긴 알림이 라벨상 안 바뀐 필드면 가짜 알림이다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from noticedelta.appspec import AppDefinition
from noticedelta.eligibility import Judgment, RuleGroup, judge
from noticedelta.evaluation.dataset import Chain
from noticedelta.evaluation.metrics import POSITIVE, Label
from noticedelta.impact import Extractor, FieldValue, Subscription, keyword_detector, run_impact
from noticedelta.llm.gateway import BudgetExceeded, MissingCassette
from noticedelta.llm.openai_compat import ProviderError

_STOPS = (MissingCassette, ProviderError, BudgetExceeded)
INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class AlertCard:
    event_id: str  # 이후 사업 버전 ID(라벨과 같은 값)
    item_id: str
    person_id: str
    kind: str  # field_changed / judgment_changed
    field: str
    before: Any
    after: Any
    confidence: float
    needs_review: bool = False  # 근거 신뢰도가 낮아 확인 필요
    held: bool = False  # 대량 영향으로 붙잡힘

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.event_id, self.person_id, self.kind, self.field)

    def shown(self) -> tuple:
        """비교할 때 보는 내용(같은 열쇠인데 이것이 다르면 '바뀐 알림')."""
        return (repr(self.before), repr(self.after), self.needs_review, self.held)


@dataclass
class Backtest:
    app: str  # "grant-alerts@3"
    cards: list[AlertCard]
    events: int
    judged: int
    missing: list[str] = field(default_factory=list)  # 녹화본이 없어 못 돌린 정정
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    planned_calls: int = 0  # PLAN 게이트웨이일 때: 보내야 할 호출과 입력 토큰 어림
    planned_tokens: int = 0

    def summary(self) -> dict[str, int]:
        c = Counter(card.kind for card in self.cards)
        return {
            "알림": len(self.cards),
            "값 변경 알림": c["field_changed"],
            "판정 변경 알림": c["judgment_changed"],
            "확인 필요": sum(card.needs_review for card in self.cards),
            "붙잡힘": sum(card.held for card in self.cards),
            "알림이 난 정정": len({card.event_id for card in self.cards}),
        }


def _judgments(
    fields: Mapping[str, FieldValue], rules_field: str | None, profiles: Mapping[str, Mapping]
) -> dict[str, Judgment]:
    fv = fields.get(rules_field) if rules_field else None
    if fv is None:
        return {}
    try:
        rules = RuleGroup.model_validate(fv.value)
    except ValueError:
        return {}
    return {p: judge(rules, attrs) for p, attrs in profiles.items()}


def backtest(
    app: AppDefinition,
    chains: Sequence[Chain],
    extractor: Extractor,
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    usage: Callable[[], Any] | None = None,
) -> Backtest:
    """usage: 게이트웨이의 사용량을 주는 함수(gateway.usage). 주면 호출·토큰을 센다."""
    start = _usage(usage)
    types = app.value_types()
    detector = keyword_detector(app.keyword_patterns(), types)
    rules_field = app.subjects.rules_field
    subs = [Subscription(p, set(types)) for p in profiles]
    hold = app.alerts.hold
    cards: list[AlertCard] = []
    missing: list[str] = []
    events = judged = 0
    for chain in chains:
        events += len(chain.events)
        try:
            fields = extractor(set(types), chain.first_segments) if chain.first_segments else {}
        except _STOPS:
            missing += [ev.event_id for ev in chain.events]
            continue
        judgments = _judgments(fields, rules_field, profiles)
        for i, ev in enumerate(chain.events):
            try:
                res = run_impact(
                    item_id=chain.item_id,
                    to_version_id=ev.event_id,
                    change=ev.change,
                    old_fields=fields,
                    extractor=extractor,
                    subscriptions=subs,
                    profiles=profiles,
                    old_judgments=judgments,
                    candidate_detector=detector,
                    old_segments=ev.old_segments,
                    unknown_lineages=ev.unknown,
                    app_id=app.id,
                    app_version=app.version,
                    alert_fields=app.alert_fields(),
                    verdict_alerts=app.alerts_on_verdict(),
                )
            except _STOPS:
                # 이 뒤의 정정은 앞 상태를 모르므로 함께 못 돌린다
                missing += [e.event_id for e in chain.events[i:]]
                break
            judged += 1
            fields = res.fields
            if rules_field and rules_field in res.real_changes:
                judgments = _judgments(fields, rules_field, profiles) or judgments
            lost = sum(a.kind == "judgment_changed" and a.after == INELIGIBLE for a in res.alerts)
            mass = lost > hold.mass_impact
            cards += [
                AlertCard(
                    event_id=ev.event_id,
                    item_id=chain.item_id,
                    person_id=a.person_id,
                    kind=a.kind,
                    field=a.field,
                    before=a.before,
                    after=a.after,
                    confidence=a.confidence,
                    needs_review=a.confidence < hold.min_confidence,
                    held=mass and a.kind == "judgment_changed" and a.after == INELIGIBLE,
                )
                for a in res.alerts
            ]
    end = _usage(usage)
    return Backtest(
        app=f"{app.id}@{app.version}",
        cards=cards,
        events=events,
        judged=judged,
        missing=missing,
        **{k: end[k] - start[k] for k in end},
    )


def _usage(usage: Callable[[], Any] | None) -> dict[str, int]:
    keys = ("calls", "input_tokens", "output_tokens", "planned_calls", "planned_tokens")
    u = usage() if usage else None
    return {k: int(getattr(u, k, 0) or 0) for k in keys}


# --- 비교 ---


@dataclass
class Comparison:
    new: list[AlertCard]  # 초안에만 있는 알림
    gone: list[AlertCard]  # 켠 판에만 있던 알림
    changed: list[tuple[AlertCard, AlertCard]]  # 같은 알림인데 내용(값·확인 필요·붙잡힘)이 달라짐
    broken: list[AlertCard]  # 깨진 시험: 맞던 알림이 사라짐
    fake_new: list[AlertCard]  # 새로 생긴 알림 중 라벨상 안 바뀐 필드(가짜 알림)
    right_new: list[AlertCard]  # 새로 생긴 알림 중 라벨상 바뀐 필드(맞는 알림)


def correct(card: AlertCard, labels: Mapping[tuple[str, str], Label]) -> bool | None:
    """라벨로 본 알림의 맞고 틀림. 라벨이 없으면 None."""
    label = labels.get((card.event_id, card.field))
    return None if label is None else label.status == POSITIVE


def compare(
    a: Backtest, b: Backtest, labels: Mapping[tuple[str, str], Label] | None = None
) -> Comparison:
    labels = labels or {}
    ia, ib = {c.key: c for c in a.cards}, {c.key: c for c in b.cards}
    new = [ib[k] for k in sorted(ib.keys() - ia.keys())]
    gone = [ia[k] for k in sorted(ia.keys() - ib.keys())]
    changed = [
        (ia[k], ib[k]) for k in sorted(ia.keys() & ib.keys()) if ia[k].shown() != ib[k].shown()
    ]
    return Comparison(
        new=new,
        gone=gone,
        changed=changed,
        broken=[c for c in gone if correct(c, labels)],
        fake_new=[c for c in new if correct(c, labels) is False],
        right_new=[c for c in new if correct(c, labels)],
    )


def by_change(cards: Sequence[AlertCard]) -> list[tuple[tuple[str, str, str], int]]:
    """사람마다 나는 알림을 (정정, 종류, 필드)로 묶어 사람 수를 센다(목록을 짧게)."""
    c = Counter((card.event_id, card.kind, card.field) for card in cards)
    return sorted(c.items())


def draft_with_alert_fields(app: AppDefinition, fields: Sequence[str]) -> AppDefinition:
    """값 변경을 알릴 필드만 바꾼 초안(판 +1). 필드 정의는 그대로라 녹화본을 그대로 쓴다."""
    from noticedelta.appspec import AlertRule

    rules = [r for r in app.alerts.rules if r.on != "field_changed"]
    if fields:
        rules.append(AlertRule(on="field_changed", fields=list(fields), to="subscribers"))
    alerts = app.alerts.model_copy(update={"rules": rules})
    return app.model_copy(update={"alerts": alerts, "version": app.version + 1})


def same_extraction(a: AppDefinition, b: AppDefinition, spec_for=None) -> bool:
    """두 판의 추출 명세(필드·안내·속성 사전·프롬프트·머리말)가 같으면 요청이 같아 녹화본을 함께 쓴다.

    spec_for: 앱 → 추출 명세(팩이 자기 프롬프트를 주는 경우). 없으면 앱 정의만으로 만든 명세로 본다.
    """
    from noticedelta.extraction import spec_for_app

    make = spec_for or spec_for_app
    return make(a) == make(b)
