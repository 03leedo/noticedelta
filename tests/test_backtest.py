"""미리 돌려 보기와 판 비교 (app-spec 10.1·10.2)."""

import itertools

from test_impact import OLD_DOC, RULES_60, RULES_84

from noticedelta import grants
from noticedelta.backtest import (
    backtest,
    by_change,
    compare,
    draft_with_alert_fields,
    same_extraction,
)
from noticedelta.evaluation.dataset import Chain, EvalEvent
from noticedelta.evaluation.metrics import Label
from noticedelta.impact import FieldValue
from noticedelta.llm.gateway import MissingCassette
from noticedelta.matching import match_segments
from noticedelta.segments import segment_document


class ByText:
    """글에 따라 값을 주는 가짜 추출기: 마감과 자격이 글에서 바로 읽힌다."""

    def __call__(self, fields, segments):
        text = " ".join(s.text for s in segments)
        src = [s.lineage_id for s in segments]
        out = {}
        if "deadline" in fields and "마감" in text:
            d = "2026-11-07 18:00" if "11월 7일" in text else "2026-10-31 18:00"
            out["deadline"] = FieldValue("deadline", "datetime", d, src, confidence=0.9)
        if "eligibility_rules" in fields and "창업" in text:
            rules = RULES_60 if "5년" in text else RULES_84
            out["eligibility_rules"] = FieldValue("eligibility_rules", "rules", rules, src, 0.9)
        if "contact" in fields and "문의" in text:
            phone = "051-2" if "051-2" in text else "051-1"
            out["contact"] = FieldValue("contact", "text", phone, src, confidence=0.9)
        return out


class NoCassette:
    def __call__(self, fields, segments):
        raise MissingCassette("k")


def chain(*docs):
    counter = itertools.count(1)
    new_id = lambda: f"L{next(counter)}"  # noqa: E731
    first = match_segments([], segment_document([docs[0]], source="body"), new_id=new_id).segments
    events, old = [], first
    for i, doc in enumerate(docs[1:], start=2):
        change = match_segments(old, segment_document([doc], source="body"), new_id=new_id)
        events.append(EvalEvent(f"V{i}", "N1", old, change))
        old = change.segments
    return Chain("N1", first, events)


DOC = OLD_DOC[0] + "\n\n○ 문의: 051-1"
PROFILES = {"young": {"business_age_months": 36}, "old": {"business_age_months": 72}}


def test_backtest_lists_alerts_the_app_would_send():
    docs = [
        DOC,
        DOC.replace("10월 31일", "11월 7일").replace("051-1", "051-2"),
        DOC.replace("7년", "5년"),
    ]
    res = backtest(grants.APP, [chain(*docs)], ByText(), PROFILES)
    assert (res.events, res.judged, res.missing) == (2, 2, [])
    # V2: 마감 변경은 두 사람 모두에게. 문의처는 이 앱이 알리지 않는다(alerts.rules)
    # V3: 7년 → 5년이라 업력 72개월인 사람만 판정이 바뀐다(마감은 다시 10월 31일로)
    assert by_change(res.cards) == [
        (("V2", "field_changed", "deadline"), 2),
        (("V3", "field_changed", "deadline"), 2),
        (("V3", "judgment_changed", "eligibility_rules"), 1),
    ]
    assert res.summary()["판정 변경 알림"] == 1


def test_compare_shows_new_and_gone_alerts_and_broken_tests():
    docs = [DOC, DOC.replace("10월 31일", "11월 7일").replace("051-1", "051-2")]
    on = backtest(grants.APP, [chain(*docs)], ByText(), PROFILES)
    labels = {("V2", "deadline"): Label("changed"), ("V2", "contact"): Label("unchanged")}
    # 문의처도 알리는 초안: 생긴 알림은 라벨상 가짜
    more = draft_with_alert_fields(
        grants.APP, ["deadline", "amount", "required_documents", "contact"]
    )
    assert more.version == grants.APP.version + 1 and same_extraction(grants.APP, more)
    diff = compare(on, backtest(more, [chain(*docs)], ByText(), PROFILES), labels)
    assert [(c.field, c.person_id) for c in diff.new] == [("contact", "old"), ("contact", "young")]
    assert len(diff.fake_new) == 2 and diff.gone == [] and diff.broken == []
    # 마감을 알리지 않는 초안: 맞던 알림이 사라진다(깨진 시험)
    fewer = draft_with_alert_fields(grants.APP, ["amount"])
    diff = compare(on, backtest(fewer, [chain(*docs)], ByText(), PROFILES), labels)
    assert len(diff.gone) == 2 and len(diff.broken) == 2 and diff.new == []


def test_missing_cassettes_are_counted_not_guessed():
    res = backtest(
        grants.APP, [chain(DOC, DOC.replace("10월 31일", "11월 7일"))], NoCassette(), PROFILES
    )
    assert res.cards == [] and res.missing == ["V2"] and res.judged == 0
