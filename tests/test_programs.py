import itertools
from datetime import datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import models
from noticedelta.programs import load_snapshots, title_key
from noticedelta.snapshot import run_snapshot

PDF = b"%PDF-1.7 attachment"
HOST = "https://www.bizinfo.go.kr"


def item(pid, title, org, deadline="2026-11-07", files=()):
    return {
        "pblancId": pid,
        "pblancNm": title,
        "excInsttNm": org,
        "reqstBeginEndDe": f"2026-10-13 ~ {deadline}",
        "bsnsSumryCn": "<p>(가상) 지원사업</p>",
        "fileNm": "@".join(files) or None,
        "flpthNm": "@".join(f"{HOST}/f/{f}" for f in files) or None,
    }


class Runs:
    """수집기(run_snapshot)를 가상 응답으로 돌려 실제와 같은 형태의 실행 파일을 만든다."""

    def __init__(self, root):
        self.root = root
        self.clock = itertools.count()
        self.http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PDF))
        )

    def run(self, items):
        from noticedelta.sources.bizinfo import to_record

        records = [to_record(i) for i in items]
        t = datetime(2026, 10, 23) + timedelta(hours=6 * next(self.clock))
        return run_snapshot(
            {"bizinfo": lambda page: records if page == 1 else []},
            http=self.http,
            root=self.root,
            now=lambda: t,
        )


@pytest.fixture
def env(tmp_path):
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    root = tmp_path / "snapshots"
    with Session(engine) as session:
        yield Runs(root), session, LocalBlobStore(root / "blobs")


def count(session, model):
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_title_key_drops_revision_marks_but_keeps_rounds():
    assert title_key("[수정공고] 2026년 창업 지원 모집") == title_key("2026년 창업 지원 모집")
    assert title_key("2026년 창업 지원 모집(정정)") == title_key("2026년 창업 지원 모집")
    assert title_key("2026년 창업 지원 2차 모집") != title_key("2026년 창업 지원 1차 모집")
    # 실제 표본에서 놓쳤던 형태
    assert title_key("[경북] 중대재해 예방 지원사업 모집 공고") == title_key(
        "[경북] 중대재해 예방 지원사업 모집 변경 공고"
    )
    assert title_key("컨설팅 지원사업 시행계획 공고") == title_key(
        "컨설팅 지원사업 시행계획 (변경 공고)"
    )
    assert title_key("[제주] 식품진흥기금 융자 계획") != title_key("[광주] 식품진흥기금 융자 계획")


def test_first_load_makes_one_program_per_posting_with_blobs(env):
    runs, session, store = env
    runs.run(
        [
            item("P1", "2026년 창업 지원 모집", "기관X", files=["공고문.hwp"]),
            item("P2", "청년 고용 지원", "기관Y"),
        ]
    )
    s = load_snapshots(session, runs.root, store)
    assert (s.runs, s.new_postings, s.new_programs, s.new_program_versions) == (1, 2, 2, 2)
    att = session.scalar(sa.select(models.Attachment))
    assert att.blob_key and att.lineage_key == "attachment:공고문:0"
    assert load_snapshots(session, runs.root, store).runs == 0  # 같은 실행은 다시 넣지 않는다


def test_bizinfo_repost_is_linked_to_the_same_program(env):
    runs, session, store = env
    runs.run([item("P1", "2026년 창업 지원 모집", "기관X")])
    runs.run([item("P9", "[수정공고] 2026년 창업 지원 모집", "기관X", deadline="2026-11-14")])
    s = load_snapshots(session, runs.root, store)
    assert s.new_programs == 1 and s.links["repost_title"] == 1 and s.closed == 1
    p1 = session.scalar(sa.select(models.Notice).where(models.Notice.external_id == "P1"))
    p9 = session.scalar(sa.select(models.Notice).where(models.Notice.external_id == "P9"))
    assert p1.program_id == p9.program_id and p1.status == "closed"
    versions = session.scalars(
        sa.select(models.ProgramVersion.version_no).where(
            models.ProgramVersion.program_id == p1.program_id
        )
    ).all()
    assert sorted(versions) == [1, 2]


def test_similar_title_is_not_merged_but_queued_for_review(env):
    runs, session, store = env
    runs.run([item("P1", "2026년 지역 창업 지원사업 참여기업 모집", "기관X")])
    runs.run(
        [
            item("P1", "2026년 지역 창업 지원사업 참여기업 모집", "기관X"),
            item("P2", "2026년 지역 창업 지원사업 참여 기업 모집 (부산)", "기관X"),
        ]
    )
    s = load_snapshots(session, runs.root, store)
    assert s.new_programs == 2 and s.links["needs_review"] == 1
    review = session.scalar(
        sa.select(models.ProgramLink).where(models.ProgramLink.decision == "needs_review")
    )
    assert 0.8 <= review.score < 1.0


def test_different_org_is_never_merged(env):
    runs, session, store = env
    runs.run([item("P1", "2026년 창업 지원 모집", "기관X")])
    runs.run([item("P2", "2026년 창업 지원 모집", "기관Z")])
    assert load_snapshots(session, runs.root, store).new_programs == 2


def test_unchanged_content_adds_no_versions_and_updates_last_seen(env):
    runs, session, store = env
    runs.run([item("P1", "2026년 창업 지원 모집", "기관X")])
    runs.run([item("P1", "2026년 창업 지원 모집", "기관X")])
    s = load_snapshots(session, runs.root, store)
    assert (s.runs, s.new_posting_versions, s.new_program_versions) == (2, 1, 1)
    snap = session.scalar(sa.select(models.SnapshotRecord))
    assert snap.last_seen_at > snap.first_seen_at
    assert count(session, models.SnapshotRecord) == 1


def test_loading_in_two_steps_equals_loading_at_once(env):
    runs, session, store = env
    runs.run([item("P1", "2026년 창업 지원 모집", "기관X")])
    load_snapshots(session, runs.root, store)
    runs.run([item("P9", "2026년 창업 지원 모집(정정)", "기관X", deadline="2026-11-30")])
    s = load_snapshots(session, runs.root, store)
    assert s.runs == 1 and s.links["repost_title"] == 1
    assert count(session, models.Program) == 1


def test_region_city_or_round_differences_are_not_even_reviewed(env):
    runs, session, store = env
    runs.run(
        [
            item("P1", "[경북] 영양군 2026년 소상공인 카드수수료 지원사업 참여점포 모집", "기관X"),
            item("P2", "2026년 2차 혁신의료기기 실증 시험평가 지원", "기관X"),
        ]
    )
    runs.run(
        [
            item("P3", "[경북] 영덕군 2026년 소상공인 카드수수료 지원사업 참여점포 모집", "기관X"),
            item("P4", "2026년 3차 혁신의료기기 실증 시험평가 지원", "기관X"),
        ]
    )
    s = load_snapshots(session, runs.root, store)
    assert s.new_programs == 4 and s.links["needs_review"] == 0
