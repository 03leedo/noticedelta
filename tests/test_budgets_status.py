"""앱별 LLM 예산과 앱 상태 보고 (app-spec 9·11장)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from noticedelta import app_registry, budgets, flight
from noticedelta.agent import build_gateway
from noticedelta.app_status import app_status, describe
from noticedelta.db import make_engine, models
from noticedelta.grants import APP
from noticedelta.llm.gateway import Budget, BudgetExceeded

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)
DAY = datetime(2026, 11, 26, 15, tzinfo=UTC)  # 한국 시간 11/27 0시


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'b.db'}")
    models.Base.metadata.create_all(engine)
    return sessionmaker(engine)


def other(app_id="mine"):
    data = APP.model_dump(mode="json", by_alias=True, exclude_none=True)
    data.update(id=app_id, owner="person:p1")
    return models.AppDefinitionRow, data


def used(s, ref, tokens, at=T0):
    flight.open_trace(s, "T", kind="worker_run", started_at=at, config={})
    flight.add_record(s, "T", "llm_usage", ref=ref, at=at, cost=tokens)


def test_each_app_has_its_own_cap_under_the_daily_total(factory):
    from noticedelta.appspec import AppDefinition

    mine = AppDefinition.model_validate(other()[1])
    with factory() as s:
        used(s, "worker", 150_000)  # 주 앱이 오늘 많이 썼다
        used(s, "app:mine", 15_000)
        root, per = budgets.plan(s, [APP, mine], primary=APP.id, daily=200_000, since=DAY)
    assert root.limit == 35_000  # 하루 전체에서 남은 것
    assert per[APP.id].limit == 50_000 and per["mine"].limit == 5_000  # 앱 한도에서 남은 것
    per["mine"].check(4_000)
    with pytest.raises(BudgetExceeded):
        per["mine"].check(6_000)  # 앱 한도에 걸린다
    per[APP.id].charge(30_000)
    assert root.spent == 30_000  # 앱이 쓴 만큼 전체에서도 빠진다
    with pytest.raises(BudgetExceeded):
        per[APP.id].check(10_000)  # 앱 한도는 남았지만 하루 전체에 걸린다
    assert root.blocked
    with factory() as s:
        app_registry.set_daily_tokens(s, "mine", 50_000, by="person:p1", at=T0)
        assert budgets.cap_of(s, "mine", primary=APP.id, daily=200_000) == 50_000
    child = Budget(10, parent=Budget(5))
    with pytest.raises(BudgetExceeded):
        child.check(7)


def test_status_report_speaks_plainly(factory, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    for stamp, err in (("20261126T020000", None), ("20261127T020000", "503"),
                       ("20261127T080000", None)):  # fmt: skip
        body = {"sources": {"bizinfo": {"error": err, "attachment_errors": 1, "changed": 2},
                            "kstartup": {"error": None, "attachment_errors": 0}}}  # fmt: skip
        (runs / f"{stamp}.json").write_text(json.dumps(body), encoding="utf-8")
    with factory() as s:
        used(s, "worker", 12_345)
        s.add(models.Source(id="bizinfo", name="기업마당"))
        s.add(models.Item(id="i1", title="t", title_key="t", current_version_no=1))
        s.flush()
        s.add(models.OutboxAlert(idempotency_key="k", person_id="p", item_id="i1", status="held",
                                 app_id=APP.id, app_version=1))  # fmt: skip
        st = app_status(s, APP, root=tmp_path, since=T0 - timedelta(days=2), now=T0,
                        primary=APP.id, daily=200_000, today=DAY)  # fmt: skip
    assert st["collect"]["runs"] == 3 and st["collect"]["sources"]["bizinfo"]["failed"] == 1
    assert st["alerts"]["held"] == 1 and st["budget"]["today"] == 12_345
    text = "\n".join(describe(st))
    assert "출처가 멈춘 적이 있습니다(bizinfo 1번)" in text
    assert "첨부를 받지 못한 적이 3번" in text and "붙잡혀 확인을 기다리는 알림이 1개" in text
    assert "오늘 LLM 토큰 12,345 / 하루 한도 200,000" in text


def test_budget_tool_needs_the_owner_and_a_confirmation(factory):
    gate = build_gateway(factory, preview=lambda d, o: {})
    with factory() as s:
        from noticedelta.appspec import AppDefinition

        app_registry.seed(s, APP, at=T0)
        app_registry.register(s, AppDefinition.model_validate(other()[1]), at=T0)
        app_registry.add_member(s, "mine", "p1", "owner", at=T0)
        p1 = gate.open_session(s, app_id=APP.id, agent="a", person_id="p1", now=T0).id
        p2 = gate.open_session(s, app_id=APP.id, agent="a", person_id="p2", now=T0).id
        s.commit()
    with factory() as s:
        out = gate.call(s, p1, "set_app_budget", {"app_id": "mine", "daily_tokens": 60_000},
                        now=T0)["action"]  # fmt: skip
        assert out["status"] == "held"  # 예산 올리기는 늘 확인
        assert "blocked" in gate.call(s, p2, "set_app_budget",
                                      {"app_id": "mine", "daily_tokens": 1}, now=T0)  # fmt: skip
        assert "blocked" in gate.call(s, p1, "set_app_budget",
                                      {"app_id": APP.id, "daily_tokens": 1}, now=T0)  # fmt: skip
        gate.approve(s, out["id"], by="person:p1", now=T0)
        s.commit()
        gate.commit_due(s, now=T0 + timedelta(minutes=10))
        assert app_registry.daily_tokens(s, "mine") == 60_000
        # 상태 보고: 운영자의 앱은 누구나, 남의 앱은 소유자만
        assert "summary" in gate.call(s, p2, "app_status", {"app_id": APP.id}, now=T0)
        assert "blocked" in gate.call(s, p2, "app_status", {"app_id": "mine"}, now=T0)
        assert "없는 앱" in gate.call(s, p1, "set_app_budget", {"app_id": "nope", "daily_tokens": 1},
                                     now=T0)["blocked"]["reason"]  # fmt: skip
