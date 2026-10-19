import json
import subprocess

import httpx
import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import models
from noticedelta.ingest import ingest_record, parse_attachments, version_segments
from noticedelta.parsing import (
    DockerParser,
    ParsedDocument,
    StaticParser,
    review_reason,
    to_blocks,
)
from noticedelta.segments import Table, TextBlock, segment_document
from noticedelta.sources import AttachmentRef, NoticeRecord

PARA = "본 사업은 지역 창업기업의 사업화를 지원하기 위한 공고입니다. 신청 자격과 일정, 제출 서류와 평가 방법은 아래와 같으니 확인 바랍니다."
MERGED = [
    ["기술분야", "사업유형", "연구비"],
    ["분산에너지", "허브 구축", "3,860"],
    [None, "딥테크 확산", "260"],
    ["농수산", "허브 구축", "2,315"],
    [None, "딥테크 확산", "354"],
]


def ok_doc(*blocks):
    return ParsedDocument("ok", "kordoc", "4.18.13", blocks=list(blocks))


def test_merged_first_column_uses_two_column_row_key_and_skips_covered_cells():
    segs = segment_document([Table(MERGED, page=3)], source="att:x")
    keys = {(s.locator["row_key"], s.locator["header"]): s.text for s in segs}
    assert keys[("분산에너지 / 딥테크 확산", "연구비")] == "260"
    assert keys[("농수산 / 딥테크 확산", "연구비")] == "354"
    assert len(segs) == 10  # 덮인 자리 2개는 조각이 아니다
    assert all(s.locator["page"] == 3 for s in segs)


def test_unmerged_table_keeps_first_column_row_key():
    segs = segment_document(
        [Table([["구분", "금액"], ["1차", "1억"], ["2차", "5천만"]])], source="s"
    )
    assert [s.locator["row_key"] for s in segs] == ["1차", "1차", "2차", "2차"]


def test_text_block_heading_and_page():
    segs = segment_document(
        [TextBlock("지원 내용", heading=True, page=2), TextBlock(PARA, page=2)],
        source="s",
        start_order=10,
    )
    assert [(s.kind, s.order, s.locator["page"]) for s in segs] == [
        ("heading", 10, 2),
        ("paragraph", 11, 2),
    ]


def test_to_blocks_maps_contract_blocks():
    doc = ok_doc(
        {"type": "heading", "text": "1. 개요", "level": 1, "page": 1},
        {"type": "paragraph", "text": PARA, "level": None, "page": 1},
        {"type": "paragraph", "text": "", "level": None, "page": 1},
        {"type": "table", "rows": MERGED, "page": 2},
    )
    blocks = to_blocks(doc)
    assert [type(b).__name__ for b in blocks] == ["TextBlock", "TextBlock", "Table"]
    assert blocks[0].heading and not blocks[1].heading and blocks[2].page == 2


@pytest.mark.parametrize(
    ("doc", "expect"),
    [
        (ok_doc({"type": "paragraph", "text": PARA}), None),
        (ParsedDocument("failed", "kordoc", "4.18.13", error="BAD"), "파싱 실패"),
        (ok_doc({"type": "paragraph", "text": "1"}), "스캔본"),
        (ok_doc({"type": "paragraph", "text": "abcdefghij" * 10}), "한글 비율"),
        (ok_doc({"type": "table", "rows": [["구분", "내용"], ["가", PARA]]}), None),
    ],
)
def test_review_reason(doc, expect):
    reason = review_reason(doc)
    assert (reason is None) if expect is None else (expect in reason)


def test_contract_version_is_checked():
    with pytest.raises(ValueError, match="계약"):
        ParsedDocument.from_json(
            {"contract": 2, "status": "ok", "parser": "x", "parser_version": "1"}
        )


def fake_run(stdout=b"", stderr=b"", code=0, seen=None, raise_timeout=False):
    def run(cmd, *, input, capture_output, timeout):
        if seen is not None:
            seen.append((cmd, input, timeout))
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

    return run


def test_docker_parser_runs_isolated_and_reads_marker_after_noise():
    seen = []
    payload = {"contract": 1, **ok_doc({"type": "paragraph", "text": PARA}).to_json()}
    out = (
        "Warning: pdfjs 잡음\n@@PARSED@@" + json.dumps(payload, ensure_ascii=False) + "\n"
    ).encode()
    doc = DockerParser(run=fake_run(out, seen=seen)).parse(b"%PDF-1.7", "application/pdf")
    assert doc.status == "ok" and doc.blocks[0]["text"] == PARA
    cmd, given, _ = seen[0]
    assert given == b"%PDF-1.7" and cmd[-1] == "application/pdf"
    for flag in ("--network", "--read-only", "--cap-drop", "--memory", "--pids-limit"):
        assert flag in cmd
    assert cmd[cmd.index("--network") + 1] == "none"


def test_docker_parser_failures():
    timeout = DockerParser(run=fake_run(raise_timeout=True), timeout_s=5).parse(
        b"x", "application/pdf"
    )
    assert timeout.status == "failed" and "초과" in timeout.error
    crashed = DockerParser(run=fake_run(b"", b"Killed", 137)).parse(b"x", "application/pdf")
    assert crashed.status == "failed" and "137" in crashed.error


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


HWPX = b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/hwp+zip" + b"\x00" * 40
PDF = b"%PDF-1.7 scanned"
ZIP = b"PK\x03\x04plain zip"


def ingest(session, store):
    files = {"공고문.hwpx": HWPX, "스캔.pdf": PDF, "서식묶음.zip": ZIP}

    def handler(request):
        return httpx.Response(200, content=files[request.url.path.rsplit("/", 1)[-1]])

    rec = NoticeRecord(
        source="bizinfo",
        external_id="PBLN_X",
        title="(가상) 공고",
        org=None,
        url=None,
        body="○ 신청기간: 2026. 10. 13. ~ 10. 31.",
        apply_start=None,
        apply_end=None,
        attachments=[
            AttachmentRef("공고문.hwpx", "https://example.test/f/공고문.hwpx", role="notice"),
            AttachmentRef("스캔.pdf", "https://example.test/f/스캔.pdf"),
            AttachmentRef("서식묶음.zip", "https://example.test/f/서식묶음.zip"),
        ],
        raw={"pblancId": "PBLN_X"},
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ingest_record(session, rec, http=http, store=store).version


def test_parse_attachments_and_version_segments(session, tmp_path):
    store = LocalBlobStore(tmp_path)
    version = ingest(session, store)
    parser = StaticParser(
        {
            "application/hwp+zip": [
                {"type": "heading", "text": "1. 지원 대상", "level": 1, "page": 1},
                {"type": "paragraph", "text": PARA, "level": None, "page": 1},
                {"type": "table", "rows": MERGED, "page": 2},
            ],
            "application/pdf": ok_doc({"type": "paragraph", "text": "  "}),
        }
    )
    outcomes = parse_attachments(session, version.id, parser=parser, store=store)
    by_name = {o.filename: o for o in outcomes}
    assert [o.filename for o in outcomes][0] == "공고문.hwpx"  # 공고문 본 파일 먼저
    assert (by_name["공고문.hwpx"].status, by_name["공고문.hwpx"].segments) == ("ok", 12)
    assert by_name["스캔.pdf"].status == "needs_review" and "스캔본" in by_name["스캔.pdf"].reason
    assert by_name["서식묶음.zip"].status == "unsupported"
    att = session.scalar(
        sa.select(models.Attachment).where(models.Attachment.filename == "공고문.hwpx")
    )
    assert att.parser == "static 0" and store.exists(att.parsed_key)

    # 다시 돌려도 이미 처리한 첨부는 파싱하지 않는다
    assert parse_attachments(session, version.id, parser=parser, store=store) == []
    assert len(parser.calls) == 2

    segs = version_segments(session, version, store)
    assert segs[0].locator["source"] == "body"
    att_segs = [s for s in segs if s.locator["source"].startswith("att:")]
    assert len(att_segs) == 12 and att_segs[0].kind == "heading"
    assert (
        att_segs[0].locator["filename"] == "공고문.hwpx" and att_segs[0].locator["role"] == "notice"
    )
    assert [s.order for s in segs] == list(range(len(segs)))
