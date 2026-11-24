import json

import httpx
import pytest

from noticedelta.llm import (
    LLMRequest,
    OpenAICompatProvider,
    ProviderError,
    choose,
    make_provider,
)

SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
    "additionalProperties": False,
}


def req(model="pickle-general"):
    return LLMRequest(
        model=model, template_version="t1", system="sys", input="입력", output_schema=SCHEMA
    )


def ok_body(content='{"x": 7}', finish="stop", usage=None):
    return {
        "choices": [
            {"finish_reason": finish, "message": {"role": "assistant", "content": content}}
        ],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 3},
    }


def provider(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("pickle-test", client=client, sleep=lambda s: None, **kw)


def test_sends_json_schema_and_parses_output():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    out = provider(handler).complete(req())
    assert out.output == {"x": 7}
    assert (out.input_tokens, out.output_tokens, out.cost_usd) == (12, 3, 0.0)
    assert seen["url"] == "https://llm.pcl.kr/v1/chat/completions"
    assert seen["auth"] == "Bearer pickle-test"
    body = seen["body"]
    assert body["model"] == "pickle-general"
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "입력"},
    ]
    assert body["response_format"]["json_schema"] == {
        "name": "output",
        "strict": True,
        "schema": SCHEMA,
    }
    assert body["temperature"] == 0.0


def test_paid_model_cost_comes_from_usage():
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0000082}
    out = provider(lambda r: httpx.Response(200, json=ok_body(usage=usage))).complete(req())
    assert out.cost_usd == pytest.approx(0.0000082)


def test_retries_rate_limit_with_retry_after():
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "5"}, json={"error": {}})
        return httpx.Response(200, json=ok_body())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = OpenAICompatProvider("pickle-test", client=client, sleep=waits.append)
    assert p.complete(req()).output == {"x": 7}
    assert waits == [5.0, 5.0]


def test_gives_up_after_retries_and_reports_code():
    def handler(request):
        return httpx.Response(429, json={"error": {"code": "credit_exhausted", "message": "한도"}})

    with pytest.raises(ProviderError, match="credit_exhausted"):
        provider(handler, retries=1).complete(req())


def test_client_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": "unsupported_parameter"}})

    with pytest.raises(ProviderError, match="unsupported_parameter"):
        provider(handler).complete(req())
    assert len(calls) == 1


@pytest.mark.parametrize(("content", "finish"), [('{"x": 1', "length"), ("말로 답함", "stop")])
def test_truncated_or_non_json_answer_is_an_error(content, finish):
    with pytest.raises(ProviderError):
        provider(lambda r: httpx.Response(200, json=ok_body(content, finish))).complete(req())


def test_empty_key_is_refused():
    with pytest.raises(ProviderError):
        OpenAICompatProvider("")


def test_choose_defaults(monkeypatch):
    for k in ("NOTICEDELTA_LLM_PROVIDER", "PICKLE_MODEL", "PICKLE_ALLOW_PAID"):
        monkeypatch.delenv(k, raising=False)
    assert choose().name == "anthropic"
    assert choose().model == "claude-haiku-5-5"
    assert choose("pickle").model == "pickle-general"
    monkeypatch.setenv("NOTICEDELTA_LLM_PROVIDER", "pickle")
    assert choose().name == "pickle"


def test_paid_pickle_models_need_opt_in(monkeypatch):
    monkeypatch.delenv("PICKLE_ALLOW_PAID", raising=False)
    monkeypatch.setenv("PICKLE_MODEL", "anthropic/claude-haiku-5.5")
    with pytest.raises(ProviderError, match="유료"):
        choose("pickle")
    monkeypatch.setenv("PICKLE_ALLOW_PAID", "1")
    assert choose("pickle").model == "anthropic/claude-haiku-5.5"


def test_unknown_provider_is_refused():
    with pytest.raises(ProviderError):
        choose("openrouter")


def test_make_pickle_provider_reads_env(monkeypatch):
    monkeypatch.setenv("PICKLE_API_KEY", "pickle-abc")
    monkeypatch.setenv("PICKLE_BASE_URL", "https://example.test/v1/")
    p = make_provider(choose("pickle"))
    assert isinstance(p, OpenAICompatProvider)
    assert p.base_url == "https://example.test/v1"


def test_quota_exhausted_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            429, headers={"retry-after": "60"}, json={"error": {"code": "quota_exhausted"}}
        )

    with pytest.raises(ProviderError, match="quota_exhausted"):
        provider(handler).complete(req())
    assert len(calls) == 1  # 기다려도 풀리지 않으므로 바로 멈춘다
