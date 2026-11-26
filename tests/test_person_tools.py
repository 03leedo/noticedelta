"""내 정보 도구(B)와 게이트웨이 MCP: 동의한 정보만, 게이트웨이를 거쳐서만 (platform-services 5.8)."""

import inspect
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import sessionmaker

from noticedelta import vault
from noticedelta.agent import build_gateway
from noticedelta.db import make_engine, models
from noticedelta.grants import APP
from noticedelta.usage import UsageLog
from noticedelta.web.demo import seed_demo

TODAY = date(2026, 10, 30)
T0 = datetime(2026, 10, 30, 9, tzinfo=UTC)
FOUNDER = {"founded_on": "2019-10-20", "ceo_birth_date": "1993-05-01", "region_sido": "부산",
           "revenue_krw": 300_000_000}  # fmt: skip


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    cipher = vault.Cipher(vault.Cipher.new_key())
    with factory() as s:
        seed_demo(s, TODAY)
        vault.put(s, cipher, "p1", FOUNDER, source="input", now=T0)
        # 에이전트 도우미에는 매출을 빼고 동의했다
        vault.grant_consent(s, "p1", APP.id, "assist", ["founded_on", "ceo_birth_date", "region_sido"],
                            text_version="v", now=T0)  # fmt: skip
        s.commit()
    gate = build_gateway(
        factory, cipher=cipher, today=lambda: TODAY, usage=UsageLog(factory, client="test")
    )
    with factory() as s:
        sid = gate.open_session(s, app_id=APP.id, agent="test", person_id="p1", now=T0).id
        s.commit()
    return factory, gate, sid


def call(env, name, **args):
    factory, gate, sid = env
    with factory() as s:
        out = gate.call(s, sid, name, args, now=T0)
        s.commit()
        return out


def test_profile_has_only_consented_keys(env):
    out = call(env, "get_profile")
    assert out["keys"] == ["ceo_birth_date", "founded_on", "region_sido"]  # 매출은 동의 밖


def test_check_and_explain_judge_with_the_vault_values(env):
    out = call(env, "check", item_id="demo-youth")
    assert out["result"] in {"eligible", "unknown"} and "revenue_krw" in out["missing"]
    exp = call(env, "explain", item_id="demo-youth")
    assert any("창업 7년 이내" in t for t in exp["sources"].values())  # 원문 근거
    assert exp["next_change"]["on"] == "2026-11-20"  # 업력 84개월을 넘는 날
    factory, gate, sid = env
    with factory() as s:
        assert s.get(models.AgentSessionRow, sid).flags == ["A", "B"]
    assert "error" in call(env, "check", item_id="nope")


def test_without_assist_consent_the_b_tools_are_blocked(env):
    factory, _, _ = env
    with factory() as s:
        vault.withdraw_consent(s, "p1", APP.id, "assist", now=T0)
        s.commit()
    assert call(env, "check", item_id="demo-youth")["blocked"]["step"] == "consent"


def test_a_session_without_a_person_cannot_read_profiles(env):
    factory, gate, _ = env
    with factory() as s:
        sid = gate.open_session(s, app_id=APP.id, agent="anon", now=T0).id
        out = gate.call(s, sid, "get_profile", {}, now=T0)
    assert "사람이 정해지지 않은" in out["blocked"]["reason"]


def test_mcp_tools_hide_the_injected_profile_and_go_through_the_gateway(env):
    pytest.importorskip("mcp")
    from noticedelta.mcp_adapter import build_gateway_server, gateway_call

    factory, gate, sid = env
    tool = gate.tools["check"]
    fn = gateway_call(gate, factory, sid, tool, lambda: T0)
    assert list(inspect.signature(fn).parameters) == ["item_id"]  # profile은 에이전트가 못 준다
    assert fn(item_id="demo-youth")["title"].startswith("(가상)")
    with factory() as s:
        decisions = s.query(models.ToolDecisionRow).filter_by(session_id=sid).all()
        assert [d.tool for d in decisions] == ["check"]
    assert build_gateway_server(gate, factory, sid) is not None


def test_usage_is_logged_without_waiting_for_the_gateway_lock(env):
    call(env, "check", item_id="demo-youth")
    call(env, "get_profile")
    factory, _, _ = env
    with factory() as s:
        tools = [r.tool for r in s.query(models.ToolCallRow).order_by(models.ToolCallRow.id)]
    assert tools == ["check", "get_profile"]


def test_closed_items_say_so_and_have_no_next_change(env):
    factory, _, _ = env
    gate = build_gateway(factory, cipher=env[1].cipher, today=lambda: date(2027, 6, 1))
    with factory() as s:
        sid = gate.open_session(s, app_id=APP.id, agent="later", person_id="p1", now=T0).id
        out = gate.call(s, sid, "explain", {"item_id": "demo-youth"}, now=T0)
    assert out["closed"] is True and out["next_change"] is None and out["dates"]


def test_a_closed_session_answers_blocked_over_mcp(env):
    pytest.importorskip("mcp")
    from noticedelta.mcp_adapter import gateway_call

    factory, gate, sid = env
    with factory() as s:
        gate.close_session(s, sid, now=T0)
        s.commit()
    fn = gateway_call(gate, factory, sid, gate.tools["get_profile"], lambda: T0)
    assert fn()["blocked"]["step"] == "session"
