import json
import logging
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
    assert [(a.filename, a.role) for a in r.attachments] == [
        ("공고문(수정).pdf", "notice"),
        ("붙임1. 신청서.hwpx", "attachment"),
        ("붙임2. 개인정보 동의서.hwp", "attachment"),
    ]
    assert r.attachments[2].url.endswith("fileSn=1")
    assert "<p>" not in r.body and "사업화를 지원합니다." in r.body
    assert "문의처: (가상) 창업지원팀" in r.body
    assert (r.apply_start, r.apply_end) == ("2026-10-13", "2026-11-07")


def page(*items, code="00"):
    return {
        "response": {
            "header": {
                "resultCode": code,
                "resultMsg": "NORMAL_SERVICE" if code == "00" else "ERR",
            },
            "body": {"items": {"item": list(items)} if items else {}, "totalCount": len(items)},
        }
    }


def test_bizinfo_empty_page():
    assert BizinfoClient(KEY, mock_http(page())).list_notices() == []


def test_bizinfo_result_code_error():
    with pytest.raises(SourceError, match="99"):
        BizinfoClient(KEY, mock_http(page(code="99"))).list_notices()


def test_bizinfo_unexpected_shape_is_an_error_not_empty():
    with pytest.raises(SourceError, match="형태"):
        BizinfoClient(KEY, mock_http({"jsonArray": [{"pblancId": "A"}]})).list_notices()


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonArray": [{"pblancId": "A", "pblancNm": "t"}]},
        {"jsonArray": {"item": [{"pblancId": "A", "pblancNm": "t"}]}},
        {"jsonArray": {"item": {"pblancId": "A", "pblancNm": "t"}}},
    ],
)
def test_bizinfo_own_api_envelopes(payload):
    recs = BizinfoClient(KEY, mock_http(payload), api="bizinfo").list_notices()
    assert [r.external_id for r in recs] == ["A"]


def test_free_text_period_is_kept_in_body_not_dates():
    payload = page({"pblancId": "A", "pblancNm": "t", "reqstBeginEndDe": "예산 소진시까지"})
    r = BizinfoClient(KEY, mock_http(payload)).list_notices()[0]
    assert r.apply_end is None
    assert "예산 소진시까지" in r.body


def test_content_hash_changes_with_content():
    p1 = load("bizinfo_page.json")
    a = BizinfoClient(KEY, mock_http(p1)).list_notices()[0]
    item = p1["response"]["body"]["items"]["item"][0]
    item["updtPnttm"], item["inqireCo"] = "2026-10-21 09:00:00", 99
    same = BizinfoClient(KEY, mock_http(p1)).list_notices()[0]
    assert same.content_hash == a.content_hash  # 일괄 수정 시각·조회수만 바뀜
    item["reqstBeginEndDe"] = "2026-10-13 ~ 2026-11-14"
    b = BizinfoClient(KEY, mock_http(p1)).list_notices()[0]
    assert a.content_hash != b.content_hash


def test_http_error_does_not_leak_key():
    with pytest.raises(SourceError) as e:
        KStartupClient(KEY, mock_http(status=500)).list_notices()
    assert KEY not in str(e.value)
    assert "500" in str(e.value)


@pytest.mark.parametrize(
    ("make", "payload"),
    [
        (lambda h: KStartupClient(KEY, h), {"data": []}),
        (lambda h: BizinfoClient(KEY, h), {"response": {"header": {"resultCode": "00"}}}),
        (lambda h: BizinfoClient(KEY, h, api="bizinfo"), {"jsonArray": []}),
    ],
)
def test_httpx_request_log_does_not_leak_key(caplog, make, payload):
    with caplog.at_level(logging.INFO, logger="httpx"):
        make(mock_http(payload)).list_notices()
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name == "httpx")
    assert "HTTP Request" in logged
    assert KEY not in logged
    assert "Key=***" in logged


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


def test_encoded_service_key_is_decoded_once():
    seen = []
    KStartupClient("abc%2Bdef%3D%3D", mock_http({"data": []}, seen=seen)).list_notices()
    assert seen[0].url.params["serviceKey"] == "abc+def=="
    seen.clear()
    BizinfoClient("abc+def==", mock_http(page(), seen=seen)).list_notices()
    assert seen[0].url.params["serviceKey"] == "abc+def=="


def test_openapi_error_code_is_reported_without_key():
    body = {
        "OpenAPI_ServiceResponse": {
            "cmmMsgHeader": {
                "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
                "returnReasonCode": "30",
            }
        }
    }

    def handler(request):
        return httpx.Response(403, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError) as e:
        KStartupClient(KEY, http).list_notices()
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(e.value) and "403" in str(e.value)
    assert KEY not in str(e.value)

    ok200 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(SourceError, match="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"):
        BizinfoClient(KEY, ok200).list_notices()


def test_kstartup_body_includes_contact_and_target_categories():
    r = KStartupClient(KEY, mock_http(load("kstartup_page.json"))).list_notices()[0]
    assert "신청 대상 구분: 일반기업,1인 창조기업" in r.body
    assert "문의처: 000-0000-0000" in r.body
