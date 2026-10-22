"""평가 문서에서 재현된 수집·파서 실행기 결함의 회귀 테스트."""

import json
import subprocess

import httpx
import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import models
from noticedelta.ingest import ingest_record, parse_attachments
from noticedelta.parsing import DockerParser, ParsedDocument, ParserUnavailable, StaticParser
from noticedelta.sources import AttachmentRef, CircuitBreaker, NoticeRecord, SourceError
from noticedelta.sources.attachments import check_url, download_attachment

PDF = b"%PDF-1.7 hello"
HOST = "https://www.bizinfo.go.kr"
TEXT = "본 사업은 지역 창업기업의 사업화를 지원합니다. 신청 자격과 일정, 제출 서류와 평가 방법은 공고문 본문과 붙임 서식을 확인하시기 바랍니다."
LINE_SEP = chr(0x2028)
NEL = chr(0x85)
LONE_SURROGATE = chr(0xD83D)


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def record(deadline="2026-11-07"):
    return NoticeRecord(
        source="bizinfo",
        external_id="PBLN_X",
        title="(가상) 공고",
        org=None,
        url=None,
        body=f"마감 {deadline}",
        apply_start=None,
        apply_end=deadline,
        attachments=[AttachmentRef("a.pdf", f"{HOST}/f/a.pdf")],
        raw={"pblancId": "PBLN_X", "end": deadline},
    )


def http_from(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- 수집 ---


def test_failed_attachment_is_retried_even_when_content_unchanged(session, tmp_path):
    store, up = LocalBlobStore(tmp_path), {"ok": False}

    def handler(request):
        return httpx.Response(200, content=PDF) if up["ok"] else httpx.Response(503)

    first = ingest_record(session, record(), http=http_from(handler), store=store)
    assert first.failed
    up["ok"] = True
    again = ingest_record(session, record(), http=http_from(handler), store=store)
    assert again.version is None and [s.filename for s in again.stored] == ["a.pdf"]
    assert session.scalar(sa.select(models.Attachment.blob_key)) is not None


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(200, content=b"x" * 100),  # 크기 초과
        lambda r: httpx.Response(200, content=b"<html>login</html>"),  # HTML
    ],
)
def test_half_open_trial_slot_is_released_on_any_failure(tmp_path, handler):
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after_s=60, clock=clock)
    breaker.record_failure()
    clock.t = 61  # half-open
    with pytest.raises(SourceError):
        download_attachment(
            http_from(handler),
            AttachmentRef("a.pdf", f"{HOST}/a.pdf"),
            LocalBlobStore(tmp_path),
            source="t",
            breaker=breaker,
            max_bytes=50,
        )
    clock.t = 200  # 다시 half-open이 되면 시험 호출을 받아야 한다
    assert breaker.allow()


def test_html_response_counts_as_source_failure(tmp_path):
    breaker = CircuitBreaker(failure_threshold=2, reset_after_s=60, clock=Clock())
    http = http_from(lambda r: httpx.Response(200, content=b"<html>error</html>"))
    for _ in range(2):
        with pytest.raises(SourceError):
            download_attachment(
                http,
                AttachmentRef("a.pdf", f"{HOST}/a.pdf"),
                LocalBlobStore(tmp_path),
                source="t",
                breaker=breaker,
            )
    assert breaker.state == "open"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bizinfo.go.kr/a.pdf",
        "https://169.254.169.254/latest/meta-data/",
        "https://evil.example/a.pdf",
        "https://www.bizinfo.go.kr/a\n.pdf",
    ],
)
def test_disallowed_urls_are_rejected_before_any_request(url):
    with pytest.raises(SourceError):
        check_url(url, frozenset({"www.bizinfo.go.kr"}))


def test_redirects_are_checked_each_hop(tmp_path):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/a.pdf":
            return httpx.Response(302, headers={"location": f"{HOST}/b.pdf"})
        if request.url.path == "/b.pdf":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})
        return httpx.Response(200, content=PDF)

    with pytest.raises(SourceError, match="허용하지 않은"):
        download_attachment(
            http_from(handler),
            AttachmentRef("a.pdf", f"{HOST}/a.pdf"),
            LocalBlobStore(tmp_path),
            source="t",
        )
    assert seen == [f"{HOST}/a.pdf", f"{HOST}/b.pdf"]  # 내부 주소로는 요청하지 않았다


# --- 파서 실행기 ---


def fake_run(stdout=b"", stderr=b"", code=0, calls=None, timeout_first=False):
    def run(cmd, *, input, capture_output, timeout):
        if calls is not None:
            calls.append(cmd)
        if timeout_first and cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

    return run


def result(text):
    doc = ParsedDocument("ok", "kordoc", "4.18.13", blocks=[{"type": "paragraph", "text": text}])
    # Node의 JSON.stringify처럼 U+2028을 이스케이프하지 않고 그대로 쓴다
    return ("\n@@PARSED@@" + json.dumps(doc.to_json(), ensure_ascii=False) + "\n").encode()


@pytest.mark.parametrize(
    "text", [TEXT + " @@PARSED@@ 표지가 본문에", TEXT + LINE_SEP + "줄 구분 문자", TEXT + NEL]
)
def test_marker_or_line_separators_in_text_do_not_break_parsing(text):
    doc = DockerParser(run=fake_run(result(text))).parse(PDF, "application/pdf")
    assert doc.status == "ok" and doc.blocks[0]["text"] == text


@pytest.mark.parametrize(
    "line",
    [
        '{"contract":1,"status":"ok","parser":"x","parser_version":"1",'
        '"blocks":[{"type":"script"}]}',
        '{"contract":1,"status":"ok","parser":"x","parser_version":"1",'
        '"blocks":[{"type":"table","rows":[[1]]}]}',
        "{not json",
    ],
)
def test_malformed_parser_output_becomes_failed_document(line):
    out = ("noise\n@@PARSED@@" + line + "\n").encode()
    doc = DockerParser(run=fake_run(out)).parse(PDF, "application/pdf")
    assert doc.status == "failed" and "출력 오류" in doc.error


def test_timeout_kills_the_named_container():
    calls = []
    parser = DockerParser(run=fake_run(calls=calls, timeout_first=True), timeout_s=5)
    doc = parser.parse(PDF, "application/pdf")
    assert doc.status == "failed"
    run_cmd, kill_cmd = calls
    name = run_cmd[run_cmd.index("--name") + 1]
    assert kill_cmd[1:] == ["kill", name]
    for flag, value in [("--tmpfs", "/tmp:size=256m"), ("--log-driver", "none")]:
        assert run_cmd[run_cmd.index(flag) + 1] == value
    assert run_cmd[run_cmd.index("--memory-swap") + 1] == run_cmd[run_cmd.index("--memory") + 1]


@pytest.mark.parametrize(
    ("code", "stderr"),
    [(125, b"Unable to find image"), (1, b"failed to connect to the docker API at npipe")],
)
def test_infra_failures_raise_parser_unavailable(code, stderr):
    with pytest.raises(ParserUnavailable):
        DockerParser(run=fake_run(b"", stderr, code)).parse(PDF, "application/pdf")


def test_missing_docker_binary_raises_parser_unavailable():
    def run(*a, **k):
        raise FileNotFoundError("docker")

    with pytest.raises(ParserUnavailable):
        DockerParser(run=run).parse(PDF, "application/pdf")


class DownParser:
    def parse(self, data, mime):
        raise ParserUnavailable("docker down")


def ingest_pdf(session, store):
    http = http_from(lambda r: httpx.Response(200, content=PDF))
    return ingest_record(session, record(), http=http, store=store).version


def test_parser_unavailable_keeps_attachments_pending(session, tmp_path):
    store = LocalBlobStore(tmp_path)
    version = ingest_pdf(session, store)
    with pytest.raises(ParserUnavailable):
        parse_attachments(session, version.id, parser=DownParser(), store=store)
    assert session.scalar(sa.select(models.Attachment.parse_status)) == "pending"
    # Docker가 돌아오면 다시 파싱한다
    ok = StaticParser({"application/pdf": [{"type": "paragraph", "text": TEXT}]})
    outcomes = parse_attachments(session, version.id, parser=ok, store=store)
    assert [o.status for o in outcomes] == ["ok"]


def test_lone_surrogate_in_parser_output_is_stored(session, tmp_path):
    store = LocalBlobStore(tmp_path)
    version = ingest_pdf(session, store)
    blocks = [{"type": "paragraph", "text": TEXT + LONE_SURROGATE}]
    parser = StaticParser({"application/pdf": blocks})
    assert parse_attachments(session, version.id, parser=parser, store=store)[0].status == "ok"
