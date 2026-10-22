"""버전 완전성: 일시 장애로 빠진 첨부가 "조건 삭제" 가짜 알림을 만들지 않게."""

import itertools

import httpx
import pytest
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import models
from noticedelta.eligibility import Judgment
from noticedelta.impact import FieldValue, run_impact
from noticedelta.ingest import ingest_record, parse_attachments, version_segments
from noticedelta.matching import match_segments
from noticedelta.parsing import StaticParser
from noticedelta.programs import (
    attachments_settled,
    comparable_old_segments,
    unreadable_attachments,
)
from noticedelta.sources import AttachmentRef, NoticeRecord

HOST = "https://www.bizinfo.go.kr"
PDF = b"%PDF-1.7 rules"
RULES_TEXT = "○ 신청 자격: 공고일 기준 창업 7년 이내 기업, 대표자 만 39세 이하. 제출 서류는 사업계획서와 사업자등록증 사본이다."


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def record(deadline):
    return NoticeRecord(
        source="bizinfo",
        external_id="P1",
        title="(가상) 공고",
        org=None,
        url=None,
        body=f"마감 {deadline}",
        apply_start=None,
        apply_end=deadline,
        attachments=[AttachmentRef("공고문.pdf", f"{HOST}/f/a.pdf", role="notice")],
        raw={"pblancId": "P1", "end": deadline},
    )


def http(ok=True):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=PDF) if ok else httpx.Response(503)
        )
    )


def test_version_with_missing_attachment_is_not_settled(session, tmp_path):
    store = LocalBlobStore(tmp_path)
    v = ingest_record(session, record("2026-11-07"), http=http(ok=False), store=store).version
    ready, waiting = attachments_settled(session, v.id)
    assert not ready and waiting == ["공고문.pdf"]


def test_settled_after_fetch_and_parse(session, tmp_path):
    store = LocalBlobStore(tmp_path)
    v = ingest_record(session, record("2026-11-07"), http=http(), store=store).version
    assert not attachments_settled(session, v.id)[0]  # 받았지만 아직 파싱 전
    parse_attachments(
        session,
        v.id,
        parser=StaticParser({"application/pdf": [{"type": "paragraph", "text": RULES_TEXT}]}),
        store=store,
    )
    assert attachments_settled(session, v.id) == (True, [])


def test_unreadable_attachment_is_unknown_not_removed(session, tmp_path):
    """v1은 첨부를 읽었고 v2는 파서가 needs_review를 냈다. 옛 조각을 삭제로 보면 안 된다."""
    store = LocalBlobStore(tmp_path)
    ids = (lambda c: lambda: f"L{next(c)}")(itertools.count(1))
    good = StaticParser({"application/pdf": [{"type": "paragraph", "text": RULES_TEXT}]})
    v1 = ingest_record(session, record("2026-11-07"), http=http(), store=store).version
    parse_attachments(session, v1.id, parser=good, store=store)
    old = match_segments([], version_segments(session, v1, store), new_id=ids).segments
    rules_lid = next(s.lineage_id for s in old if "신청 자격" in s.text)

    v2 = ingest_record(session, record("2026-11-14"), http=http(), store=store).version
    scanned = StaticParser({"application/pdf": [{"type": "paragraph", "text": " "}]})
    parse_attachments(session, v2.id, parser=scanned, store=store)
    assert attachments_settled(session, v2.id)[0]  # needs_review도 끝 상태다

    kept, unknown = comparable_old_segments(old, unreadable_attachments(session, v2.id))
    assert rules_lid in unknown
    change = match_segments(kept, version_segments(session, v2, store), new_id=ids)
    assert rules_lid not in change.removed

    rules = {
        "group": "all_of",
        "rules": [{"attribute": "business_age_months", "op": "<=", "value": 84}],
    }
    fields = {"eligibility_rules": FieldValue("eligibility_rules", "rules", rules, [rules_lid])}
    res = run_impact(
        program_id="PG1",
        to_version_id="v2",
        change=change,
        old_fields=fields,
        extractor=lambda f, s: {},
        subscriptions=[],
        profiles={"u1": {"business_age_months": 30}},
        old_judgments={"u1": Judgment("eligible")},
        unknown_lineages=unknown,
    )
    assert res.stale == {"eligibility_rules"} and res.alerts == []
    assert res.fields["eligibility_rules"].confidence < 0.7
