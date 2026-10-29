import json

import httpx

from noticedelta.snapshot import PER_PAGE, canonical_item, last_hashes, run_snapshot
from noticedelta.sources import AttachmentRef, BizinfoClient, NoticeRecord, SourceError

PDF = b"%PDF-1.7 test"


def rec(ext, deadline="2026-11-07", files=(), views=1):
    return NoticeRecord(
        source="bizinfo",
        external_id=ext,
        title=f"(가상) 공고 {ext}",
        org=None,
        url=None,
        body=f"마감 {deadline}",
        apply_start=None,
        apply_end=deadline,
        attachments=[AttachmentRef(f, f"https://www.bizinfo.go.kr/f/{f}") for f in files],
        raw={
            "pblancId": ext,
            "end": deadline,
            "inqireCo": views,
            "updtPnttm": "2026-10-23 01:00:00",
        },
    )


def pages(records, per=PER_PAGE):
    def list_page(page):
        return records[(page - 1) * per : page * per]

    return list_page


def http_with(files, calls):
    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        calls.append(name)
        if name not in files:
            return httpx.Response(503)
        return httpx.Response(200, content=files[name])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_canonical_item_ignores_volatile_fields():
    assert canonical_item({"a": 1, "inqireCo": 5, "updtPnttm": "x"}) == canonical_item({"a": 1})


def test_paginates_until_short_page(tmp_path):
    many = [rec(f"P{i}") for i in range(PER_PAGE + 3)]
    s = run_snapshot({"bizinfo": pages(many)}, http=http_with({}, []), root=tmp_path)
    assert s.ok and s.sources["bizinfo"]["count"] == PER_PAGE + 3


def test_first_run_marks_all_changed_then_only_real_changes(tmp_path):
    calls = []
    http = http_with({"a.pdf": PDF}, calls)
    run_snapshot(
        {"bizinfo": pages([rec("A", files=["a.pdf"]), rec("B")])}, http=http, root=tmp_path
    )
    assert calls == ["a.pdf"]

    # 조회수만 바뀜: 변경 아님, 첨부도 다시 받지 않음
    s2 = run_snapshot(
        {"bizinfo": pages([rec("A", files=["a.pdf"], views=9), rec("B", views=9)])},
        http=http,
        root=tmp_path,
    )
    assert s2.new_or_changed == 0 and calls == ["a.pdf"]

    # 마감이 바뀜: 그 공고만 변경, 첨부를 다시 받음
    s3 = run_snapshot(
        {"bizinfo": pages([rec("A", "2026-11-14", files=["a.pdf"]), rec("B")])},
        http=http,
        root=tmp_path,
    )
    assert s3.new_or_changed == 1 and calls == ["a.pdf", "a.pdf"]
    assert last_hashes(tmp_path / "runs")["bizinfo"]["A"] == rec("A", "2026-11-14").content_hash


def test_failed_attachment_is_retried_next_run(tmp_path):
    calls = []
    files = {}
    http = http_with(files, calls)
    s1 = run_snapshot({"bizinfo": pages([rec("A", files=["a.pdf"])])}, http=http, root=tmp_path)
    assert s1.sources["bizinfo"]["attachment_errors"] == 1
    files["a.pdf"] = PDF  # 서버 복구
    s2 = run_snapshot({"bizinfo": pages([rec("A", files=["a.pdf"])])}, http=http, root=tmp_path)
    assert s2.sources["bizinfo"]["attachments_fetched"] == 1 and calls == ["a.pdf", "a.pdf"]
    run = json.loads(sorted((tmp_path / "runs").glob("*.json"))[-1].read_text(encoding="utf-8"))
    assert run["sources"]["bizinfo"]["items"][0]["attachments"][0]["blob_key"]


def test_one_source_failing_does_not_stop_the_other(tmp_path):
    def broken(page):
        raise SourceError("kstartup API 오류: HTTP 500")

    s = run_snapshot(
        {"kstartup": broken, "bizinfo": pages([rec("A")])}, http=http_with({}, []), root=tmp_path
    )
    assert not s.ok
    assert s.sources["kstartup"]["error"].endswith("500")
    assert s.sources["bizinfo"]["count"] == 1
    assert json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))["ok"] is False


def test_bizinfo_single_object_item():
    payload = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"items": {"item": {"pblancId": "A", "pblancNm": "t"}}},
        }
    }
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert [r.external_id for r in BizinfoClient("k", http).list_notices()] == ["A"]


def test_failed_run_is_not_the_baseline_for_the_next(tmp_path):
    """절전에서 깬 직후 실행이 네트워크 오류로 실패해도, 다음 실행은 그 전 성공 실행과 비교한다."""
    from datetime import datetime

    def down(page):
        raise SourceError("bizinfo API 호출 실패: ConnectError")

    def run(source, hour):
        t = datetime(2026, 10, 23, hour)
        return run_snapshot(
            {"bizinfo": source}, http=http_with({}, []), root=tmp_path, now=lambda: t
        )

    run(pages([rec("A"), rec("B")]), 1)
    run(down, 7)
    assert run(pages([rec("A"), rec("B", "2026-11-14")]), 13).new_or_changed == 1


def test_transient_failure_is_retried_then_succeeds(tmp_path):
    """절전에서 깬 직후처럼 처음 몇 번은 연결이 안 되다가 붙는 경우."""
    from noticedelta.sources import TransientSourceError

    calls = {"n": 0}

    def flaky(page):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TransientSourceError("bizinfo API 호출 실패: ConnectTimeout")
        return [rec("A")] if page == 1 else []

    waits = []
    s = run_snapshot({"bizinfo": flaky}, http=http_with({}, []), root=tmp_path, sleep=waits.append)
    assert s.ok and waits == [30, 60]
    run = json.loads(sorted((tmp_path / "runs").glob("*.json"))[-1].read_text(encoding="utf-8"))
    assert run["sources"]["bizinfo"]["attempts"] == 3


def test_gives_up_after_retries_and_permanent_errors_are_not_retried(tmp_path):
    from noticedelta.sources import TransientSourceError

    def down(page):
        raise TransientSourceError("bizinfo API 호출 실패: ConnectTimeout")

    def bad_key(page):
        raise SourceError("kstartup API 오류: HTTP 401")

    waits = []
    s = run_snapshot(
        {"bizinfo": down, "kstartup": bad_key},
        http=http_with({}, []),
        root=tmp_path,
        retry_delays=(1, 2),
        sleep=waits.append,
    )
    assert not s.ok and waits == [1, 2]  # 영구 오류(401)는 기다리지 않는다
    assert s.sources["bizinfo"]["error"].endswith("ConnectTimeout")


def test_connect_timeout_and_5xx_are_transient():
    import pytest

    from noticedelta.sources import TransientSourceError
    from noticedelta.sources.http import get_json

    def timeout(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    for handler in (timeout, lambda r: httpx.Response(503), lambda r: httpx.Response(429)):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(TransientSourceError):
            get_json(http, "https://x.test/a", {}, source="t", breaker=None, limiter=None)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(SourceError) as e:
        get_json(http, "https://x.test/a", {}, source="t", breaker=None, limiter=None)
    assert not isinstance(e.value, TransientSourceError)
