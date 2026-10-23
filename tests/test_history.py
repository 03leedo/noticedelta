"""변경 이벤트 저장과 재생 (작업 목록 11번)."""

import itertools
from datetime import datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import models
from noticedelta.history import counter_ids, record_program, replay
from noticedelta.ingest import parse_attachments
from noticedelta.parsing import ParsedDocument
from noticedelta.programs import load_snapshots
from noticedelta.snapshot import run_snapshot
from noticedelta.sources.bizinfo import to_record

HOST = "https://www.bizinfo.go.kr"
RULES = "○ 신청 자격: 공고일 기준 창업 7년 이내 기업, 대표자 만 39세 이하인 자를 대상으로 한다."
DOCS = "○ 제출 서류: 사업계획서 1부, 사업자등록증 사본 1부를 온라인으로 제출해야 한다."
SCAN = b"%PDF-1.7 scanned"


def pdf(*paras: str) -> bytes:
    return ("%PDF-1.7\n" + "\n".join(paras)).encode()


class ByContent:
    """받은 바이트의 문단을 그대로 돌려주는 파서. 스캔본은 빈 문서(needs_review)."""

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        lines = [" "] if data == SCAN else data.decode().splitlines()[1:]
        return ParsedDocument("ok", "fake", "0", [{"type": "paragraph", "text": t} for t in lines])


class Env:
    def __init__(self, tmp_path):
        engine = sqlite_engine()
        models.Base.metadata.create_all(engine)
        self.session = Session(engine)
        self.root = tmp_path / "snapshots"
        self.store = LocalBlobStore(self.root / "blobs")
        self.files: dict[str, bytes] = {}
        self.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=self.files[r.url.path])
            )
        )
        self.clock = itertools.count()

    def run(self, deadline: str, attachment: bytes):
        self.files["/f/a.pdf"] = attachment
        raw = {
            "pblancId": "P1",
            "pblancNm": "2026년 (가상) 창업 지원 모집",
            "excInsttNm": "기관X",
            "reqstBeginEndDe": f"2026-10-13 ~ {deadline}",
            "bsnsSumryCn": f"<p>접수 마감 {deadline}</p>",
            "fileNm": "공고문.pdf",
            "flpthNm": f"{HOST}/f/a.pdf",
        }
        t = datetime(2026, 10, 23) + timedelta(hours=6 * next(self.clock))
        run_snapshot(
            {"bizinfo": lambda page: [to_record(raw)] if page == 1 else []},
            http=self.http,
            root=self.root,
            now=lambda: t,
        )

    def load(self, parse=True):
        load_snapshots(self.session, self.root, self.store)
        if parse:
            for nv in self.session.scalars(sa.select(models.NoticeVersion)):
                parse_attachments(self.session, nv.id, parser=ByContent(), store=self.store)
        return self.session.scalar(sa.select(models.Program.id))


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.session.close()


def lineage_of(step, text):
    return next(s.lineage_id for s in step.change.segments if text in s.text)


def test_records_an_event_per_version_and_replays_the_same_changeset(env):
    env.run("2026-11-07", pdf(RULES, DOCS))
    env.run("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    program_id = env.load()

    rec = record_program(env.session, program_id, env.store, new_id=counter_ids())
    assert [r.status for r in rec] == ["recorded", "recorded"]
    assert rec[0].event.from_version_id is None

    steps = list(replay(env.session, program_id))
    assert len(steps) == 2
    v1, v2 = steps
    rules_lid = lineage_of(v1, "신청 자격")
    assert v1.change.added == [s.lineage_id for s in v1.change.segments]
    assert rules_lid in v2.change.modified
    assert lineage_of(v2, "제출 서류") in v2.change.unchanged
    live = rec[1].event.changed_lineages
    assert (v2.change.added, v2.change.removed, v2.change.modified) == (
        live["added"],
        live["removed"],
        live["modified"],
    )
    before, after = next((o, n) for o, n in v2.pairs() if n and n.lineage_id == rules_lid)
    assert "7년" in before.text and "5년" in after.text


def test_recording_twice_adds_nothing(env):
    env.run("2026-11-07", pdf(RULES, DOCS))
    env.run("2026-11-14", pdf(RULES, DOCS))
    program_id = env.load()
    record_program(env.session, program_id, env.store)
    n = env.session.scalar(sa.select(sa.func.count()).select_from(models.SegmentRow))
    again = record_program(env.session, program_id, env.store)
    assert [r.status for r in again] == ["exists", "exists"]
    assert env.session.scalar(sa.select(sa.func.count()).select_from(models.SegmentRow)) == n


def test_waits_until_attachments_are_settled(env):
    env.run("2026-11-07", pdf(RULES, DOCS))
    program_id = env.load(parse=False)
    rec = record_program(env.session, program_id, env.store)
    assert rec[0].status == "waiting" and rec[0].waiting == ["공고문.pdf"]
    assert env.session.scalar(sa.select(models.ChangeEvent)) is None
    env.load()
    assert record_program(env.session, program_id, env.store)[0].status == "recorded"


def test_unreadable_attachment_keeps_lineage_across_versions(env):
    """v2 첨부가 스캔본이라 읽지 못했다. v2에서는 삭제가 아니라 알 수 없음, v3에서 다시 이어진다."""
    env.run("2026-11-07", pdf(RULES, DOCS))
    env.run("2026-11-14", SCAN)
    env.run("2026-11-21", pdf(RULES, DOCS))
    program_id = env.load()
    record_program(env.session, program_id, env.store, new_id=counter_ids())

    v1, v2, v3 = replay(env.session, program_id)
    rules_lid = lineage_of(v1, "신청 자격")
    assert rules_lid in v2.unknown and rules_lid not in v2.change.removed
    # 옮겨 둔 옛 첨부 조각은 v2의 조각으로 세지 않는다. 본문 조각만 남는다.
    assert {s.locator["source"] for s in v2.change.segments} == {"body"}
    assert lineage_of(v3, "신청 자격") == rules_lid
    assert rules_lid in v3.change.unchanged and not v3.change.added
