from types import SimpleNamespace

import pytest

from noticedelta.llm import (
    AnthropicProvider,
    Budget,
    BudgetExceeded,
    FileCassetteStore,
    Gateway,
    LLMRequest,
    LLMResponse,
    MissingCassette,
    Mode,
    calibrate,
    estimate_tokens,
    request_key,
)
from noticedelta.llm.gateway import request_chars

SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
    "additionalProperties": False,
}


def req(text="hello"):
    return LLMRequest(
        model="claude-haiku-5-5",
        template_version="t1",
        system="sys",
        input=text,
        output_schema=SCHEMA,
    )


class CountingProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, r):
        self.calls += 1
        return LLMResponse(
            output={"x": self.calls}, input_tokens=100, output_tokens=10, cost_usd=0.002
        )


def test_request_key_is_stable_across_dict_order():
    a = req()
    b = LLMRequest(
        model=a.model,
        template_version="t1",
        system="sys",
        input="hello",
        output_schema={
            "additionalProperties": False,
            "required": ["x"],
            "properties": {"x": {"type": "integer"}},
            "type": "object",
        },
    )
    assert request_key(a) == request_key(b)
    assert request_key(a) != request_key(req("other"))


def test_replay_without_cassette_raises(tmp_path):
    gw = Gateway(None, FileCassetteStore(tmp_path), Mode.REPLAY)
    with pytest.raises(MissingCassette):
        gw.complete(req())


def test_record_then_replay_without_provider(tmp_path):
    store = FileCassetteStore(tmp_path)
    p = CountingProvider()
    first = Gateway(p, store, Mode.RECORD).complete(req())
    again = Gateway(None, store, Mode.REPLAY).complete(req())
    assert again.output == first.output == {"x": 1}
    assert again.from_cassette and not first.from_cassette
    assert p.calls == 1


def test_replay_or_live_uses_cassette_then_live(tmp_path):
    store = FileCassetteStore(tmp_path)
    p = CountingProvider()
    gw = Gateway(p, store, Mode.REPLAY_OR_LIVE)
    gw.complete(req("a"))
    gw.complete(req("a"))
    gw.complete(req("b"))
    assert p.calls == 2
    assert gw.usage.calls == 3
    assert gw.usage.live_calls == 2


def test_usage_totals(tmp_path):
    gw = Gateway(CountingProvider(), FileCassetteStore(tmp_path), Mode.LIVE)
    gw.complete(req("a"))
    gw.complete(req("b"))
    u = gw.usage
    assert (u.calls, u.input_tokens, u.output_tokens) == (2, 200, 20)
    assert u.cost_usd == pytest.approx(0.004)
    assert u.live_cost_usd == pytest.approx(0.004)


def test_live_without_provider_is_an_error(tmp_path):
    with pytest.raises(RuntimeError):
        Gateway(None, FileCassetteStore(tmp_path), Mode.LIVE).complete(req())


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(text='{"x": 7}', stop="end_turn"):
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    return SimpleNamespace(messages=FakeMessages(resp))


def test_anthropic_provider_uses_structured_output_and_prices():
    client = fake_client()
    out = AnthropicProvider(client).complete(req())
    assert out.output == {"x": 7}
    kw = client.messages.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert kw["system"] == "sys"
    assert out.cost_usd == pytest.approx(0.10 + 0.50)


def test_anthropic_provider_raises_on_refusal():
    with pytest.raises(RuntimeError):
        AnthropicProvider(fake_client(stop="refusal")).complete(req())


def test_plan_counts_missing_calls_once_without_sending(tmp_path):
    store = FileCassetteStore(tmp_path)
    p = CountingProvider()
    Gateway(p, store, Mode.RECORD).complete(req("a"))  # 녹화본이 있는 요청
    gw = Gateway(p, store, Mode.PLAN, tokens_per_char=1.0)
    assert gw.complete(req("a")).output == {"x": 1}  # 녹화본은 그대로 쓴다
    assert gw.complete(req("bb")).output == {}  # 없는 것은 보내지 않고 빈 응답
    gw.complete(req("bb"))  # 실제로 돌리면 두 번째는 방금 녹화한 것을 쓴다
    assert p.calls == 1
    assert gw.usage.planned_calls == 1
    assert gw.usage.planned_tokens == estimate_tokens(req("bb"), 1.0)
    assert gw.usage.live_calls == 0


def test_budget_blocks_a_call_before_sending_and_is_shared(tmp_path):
    p = CountingProvider()  # 호출마다 입력 100 + 출력 10 토큰
    budget = Budget(limit=250)
    a = Gateway(p, FileCassetteStore(tmp_path / "a"), Mode.LIVE, budget=budget)
    b = Gateway(p, FileCassetteStore(tmp_path / "b"), Mode.LIVE, budget=budget)
    a.complete(req("x"))
    b.complete(req("y"))
    assert budget.spent == 220 and a.usage.live_tokens == 110
    with pytest.raises(BudgetExceeded):
        a.complete(req("z" * 100))  # 어림 70토큰이면 250을 넘는다
    assert p.calls == 2


def test_calibrate_reads_the_token_per_char_ratio_from_cassettes(tmp_path):
    store = FileCassetteStore(tmp_path)
    Gateway(CountingProvider(), store, Mode.RECORD).complete(req("a"))
    ratio = calibrate(store.records(), "claude-haiku-5-5", "t1")
    assert ratio == pytest.approx(100 / request_chars(req("a")))
    assert calibrate(store.records(), "other-model", "t1") is None
