import json
from pathlib import Path

import httpx
import pytest

from noticedelta.sources import (
    BizinfoClient,
    CircuitBreaker,
    CircuitOpen,
    KStartupClient,
    RateLimiter,
    SourceError,
)

FIX = Path(__file__).parent / "fixtures"
KEY = "SECRET-KEY-123"


def mock_http(payload=None, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_kstartup_maps_fields():
    seen = []
    client = KStartupClient(KEY, mock_http(load("kstartup_page.json"), seen=seen))
    recs = client.list_notices(page=1, per_page=10)
    r = recs[0]
    assert (r.source, r.external_id) == ("kstartup", "900001")
    assert r.title.startswith("(가상) 2026년")
    assert r.org == "(가상) 지역창업진흥원"
    assert (r.apply_start, r.apply_end) == ("2026-10-13", "2026-10-31")
    assert "공고일 기준 창업 7년 이내 기업" in r.body
    assert r.attachments == []
    assert recs[1].apply_end is None
    q = seen[0].url.params
    assert q["returnType"] == "json" and q["page"] == "1" and q["perPage"] == "10"
    assert seen[0].url.path.endswith("/getAnnouncementInformation01")


def test_kstartup_accepts_nested_data_envelope():
    payload = load("kstartup_page.json")
    payload["data"] = {"data": payload["data"]}
    assert len(KStartupClient(KEY, mock_http(payload)).list_notices()) == 2


def test_bizinfo_maps_attachments_and_strips_html():
    r = BizinfoClient(KEY, mock_http(load("bizinfo_page.json"))).list_notices()[0]
    assert (r.source, r.external_id) == ("bizinfo", "PBLN_000000009000001")
    assert [a.filename for a in r.attachments] == ["공고문(수정).hwp", "붙임1. 신청서.hwpx"]
    assert r.attachments[1].url.endswith("fileSn=1")
    assert "<p>" not in r.body and "사업화를 지원합니다." in r.body
    assert (r.apply_start, r.apply_end) == ("2026-10-13", "2026-11-07")


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonArray": {"item": [{"pblancId": "A", "pblancNm": "t"}]}},
        {"jsonArray": {"item": {"pblancId": "A", "pblancNm": "t"}}},
        {"response": {"body": {"items": {"item": [{"pblancId": "A", "pblancNm": "t"}]}}}},
    ],
)
def test_bizinfo_envelopes(payload):
    assert [r.external_id for r in BizinfoClient(KEY, mock_http(payload)).list_notices()] == ["A"]


def test_bizinfo_empty_envelope():
    assert BizinfoClient(KEY, mock_http({"jsonArray": {"item": None}})).list_notices() == []


def test_free_text_period_is_kept_in_body_not_dates():
    payload = {
        "jsonArray": [{"pblancId": "A", "pblancNm": "t", "reqstBeginEndDe": "예산 소진시까지"}]
    }
    r = BizinfoClient(KEY, mock_http(payload)).list_notices()[0]
    assert r.apply_end is None
    assert "예산 소진시까지" in r.body


def test_content_hash_changes_with_content():
    p1 = load("bizinfo_page.json")
    a = BizinfoClient(KEY, mock_http(p1)).list_notices()[0]
    p1["jsonArray"][0]["reqstBeginEndDe"] = "2026-10-13 ~ 2026-11-14"
    b = BizinfoClient(KEY, mock_http(p1)).list_notices()[0]
    assert a.content_hash != b.content_hash


def test_http_error_does_not_leak_key():
    with pytest.raises(SourceError) as e:
        KStartupClient(KEY, mock_http(status=500)).list_notices()
    assert KEY not in str(e.value)
    assert "500" in str(e.value)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_circuit_breaker_opens_and_half_opens():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=5, reset_after_s=60, clock=clock)
    for _ in range(5):
        assert cb.allow()
        cb.record_failure()
    assert not cb.allow()
    clock.t = 61
    assert cb.allow()  # half-open: 한 번 시도 허용
    cb.record_success()
    assert cb.allow()


def test_client_stops_when_circuit_open():
    cb = CircuitBreaker(failure_threshold=1, reset_after_s=60, clock=FakeClock())
    client = KStartupClient(KEY, mock_http(status=500), breaker=cb)
    with pytest.raises(SourceError):
        client.list_notices()
    with pytest.raises(CircuitOpen):
        client.list_notices()


def test_rate_limiter_spaces_calls():
    clock = FakeClock()
    slept = []

    def sleep(s):
        slept.append(s)
        clock.t += s

    rl = RateLimiter(rate_per_sec=2, clock=clock, sleep=sleep)
    rl.acquire()
    rl.acquire()
    rl.acquire()
    assert slept == [0.5, 0.5]


def test_half_open_allows_a_single_trial():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, reset_after_s=60, clock=clock)
    cb.record_failure()
    clock.t = 61
    assert cb.allow()
    assert not cb.allow()  # 시험 호출 결과가 나오기 전에는 하나만
    cb.record_failure()
    assert not cb.allow()  # 다시 열림
    clock.t = 122
    assert cb.allow()
    cb.record_success()
    assert cb.allow() and cb.allow()
