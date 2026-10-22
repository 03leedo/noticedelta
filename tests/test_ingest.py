import httpx
import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.blobstore import LocalBlobStore, blob_key
from noticedelta.db import models
from noticedelta.ingest import ingest_record
from noticedelta.sources import AttachmentRef, CircuitBreaker, NoticeRecord, SourceError
from noticedelta.sources.attachments import download_attachment, sniff_mime

HWP = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
HWPX = b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/hwp+zip" + b"\x00" * 50
PDF = b"%PDF-1.7\n..."
BASE = "https://www.bizinfo.go.kr/files/"


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def http_with(files, calls=None):
    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(name)
        if name not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=files[name])

    return httpx.Client(transport=httpx.MockTransport(handler))


def record(attachments, deadline="2026-11-07"):
    return NoticeRecord(
        source="bizinfo",
        external_id="PBLN_X",
        title="(가상) 공고",
        org=None,
        url=None,
        body=f"마감 {deadline}",
        apply_start=None,
        apply_end=deadline,
        attachments=[AttachmentRef(n, BASE + n) for n in attachments],
        raw={"pblancId": "PBLN_X", "end": deadline, "files": attachments},
    )


@pytest.mark.parametrize(
    ("data", "name", "mime"),
    [
        (PDF, "a.pdf", "application/pdf"),
        (HWP, "a.hwp", "application/x-hwp"),
        (HWP + b"\x00" * 5000 + b"HWP Document File", "a.hwpx", "application/x-hwp"),
        (HWP, "a.doc", "application/x-ole-storage"),
        (HWPX, "a.hwpx", "application/hwp+zip"),
        (HWPX, "a.bin", "application/hwp+zip"),
        (b"PK\x03\x04rest", "a.zip", "application/zip"),
        (b"  <!DOCTYPE html><html>", "a.hwp", "text/html"),
        (b"\x00\x01", "a.hwp", "application/octet-stream"),
    ],
)
def test_sniff_mime_uses_bytes_not_extension(data, name, mime):
    assert sniff_mime(data, name) == mime


def test_ingest_stores_attachments_once(session, tmp_path):
    store = LocalBlobStore(tmp_path)
    calls = []
    http = http_with({"공고문.hwp": HWP, "신청서.hwpx": HWPX}, calls)

    res = ingest_record(session, record(["공고문.hwp", "신청서.hwpx"]), http=http, store=store)
    assert res.version is not None and not res.failed
    rows = session.scalars(sa.select(models.Attachment).order_by(models.Attachment.filename)).all()
    assert [(r.filename, r.blob_key, r.mime) for r in rows] == [
        ("공고문.hwp", blob_key(HWP), "application/x-hwp"),
        ("신청서.hwpx", blob_key(HWPX), "application/hwp+zip"),
    ]
    assert store.get(blob_key(HWP)) == HWP

    again = ingest_record(session, record(["공고문.hwp", "신청서.hwpx"]), http=http, store=store)
    assert again.version is None
    assert len(calls) == 2  # 내용이 같으면 첨부를 다시 받지 않는다


def test_ingest_keeps_going_when_one_attachment_fails(session, tmp_path):
    http = http_with({"ok.pdf": PDF})
    res = ingest_record(
        session, record(["ok.pdf", "없음.hwp"]), http=http, store=LocalBlobStore(tmp_path)
    )
    assert [s.filename for s in res.stored] == ["ok.pdf"]
    assert [f for f, _ in res.failed] == ["없음.hwp"]
    missing = session.scalar(
        sa.select(models.Attachment).where(models.Attachment.blob_key.is_(None))
    )
    assert missing.filename == "없음.hwp"  # 다음 수집 때 다시 시도할 수 있게 비워 둔다


def test_ingest_stops_on_open_circuit(session, tmp_path):
    breaker = CircuitBreaker(failure_threshold=1, reset_after_s=60, clock=lambda: 0.0)
    http = http_with({})
    res = ingest_record(
        session,
        record(["a.hwp", "b.hwp", "c.hwp"]),
        http=http,
        store=LocalBlobStore(tmp_path),
        breaker=breaker,
    )
    assert [f for f, _ in res.failed] == ["a.hwp", "b.hwp"]
    assert res.failed[1][1] == "서킷 열림"


def test_download_rejects_html_and_oversize(tmp_path):
    store = LocalBlobStore(tmp_path)
    http = http_with({"err.hwp": b"<html>login</html>", "big.pdf": PDF + b"x" * 100})
    with pytest.raises(SourceError, match="HTML"):
        download_attachment(http, AttachmentRef("err.hwp", BASE + "err.hwp"), store, source="t")
    with pytest.raises(SourceError, match="너무 큼"):
        download_attachment(
            http, AttachmentRef("big.pdf", BASE + "big.pdf"), store, source="t", max_bytes=50
        )
    assert not list(tmp_path.rglob("*"))  # 아무것도 저장하지 않았다
