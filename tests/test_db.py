import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.db import models, repo
from noticedelta.impact import Alert
from noticedelta.sources import NoticeRecord


@pytest.fixture
def session():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def record(deadline="2026-10-31", ext="900001"):
    return NoticeRecord(
        source="kstartup",
        external_id=ext,
        title="(가상) 공고",
        org="(가상) 기관",
        url=None,
        body=f"접수 기간: 2026-10-13 ~ {deadline}",
        apply_start="2026-10-13",
        apply_end=deadline,
        raw={"pbanc_sn": ext, "pbanc_rcpt_end_dt": deadline},
    )


def test_all_design_tables_exist():
    names = set(models.Base.metadata.tables)
    assert {
        "source",
        "notice",
        "notice_version",
        "attachment",
        "segment",
        "field_value",
        "eligibility_rule",
        "profile",
        "subscription",
        "judgment",
        "change_event",
        "outbox_alert",
        "job_ledger",
    } <= names


def test_first_fetch_creates_original_version(session):
    notice, version = repo.upsert_notice_version(session, record())
    assert version.version_no == 1 and version.kind == "original"
    assert notice.current_version_id == version.id


def test_same_content_creates_no_version(session):
    repo.upsert_notice_version(session, record())
    _, version = repo.upsert_notice_version(session, record())
    assert version is None


def test_changed_content_creates_correction(session):
    repo.upsert_notice_version(session, record())
    notice, version = repo.upsert_notice_version(session, record(deadline="2026-11-07"))
    assert (version.version_no, version.kind) == (2, "correction")
    assert notice.current_version_id == version.id


def test_older_version_cannot_overwrite_newer(session):
    notice, v1 = repo.upsert_notice_version(session, record())
    _, v2 = repo.upsert_notice_version(session, record(deadline="2026-11-07"))
    assert repo.advance_current_version(session, notice.id, v1.id, v1.version_no) is False
    session.refresh(notice)
    assert notice.current_version_id == v2.id


def alert(key="A:N1:deadline:V2"):
    return Alert("A", "N1", "deadline", "V2", "field_changed", "x", "y", ["L1"], key)


def test_enqueue_alert_is_idempotent(session):
    assert repo.enqueue_alert(session, alert()) is True
    assert repo.enqueue_alert(session, alert()) is False
    assert session.scalar(sa.select(sa.func.count()).select_from(models.OutboxAlert)) == 1


def test_claim_job_once(session):
    assert repo.claim_job(session, "parse", "N1:abc") is True
    assert repo.claim_job(session, "parse", "N1:abc") is False
    assert repo.claim_job(session, "extract", "N1:abc") is True


def test_redelivered_old_snapshot_does_not_roll_back(session):
    repo.upsert_notice_version(session, record())
    notice, v2 = repo.upsert_notice_version(session, record(deadline="2026-11-07"))
    _, again = repo.upsert_notice_version(session, record())  # 늦게 도착한 옛 스냅샷
    assert again is None
    session.refresh(notice)
    assert notice.current_version_id == v2.id


def test_upsert_survives_concurrent_notice_insert(session, monkeypatch):
    """다른 워커가 같은 공고를 먼저 넣은 상황: 조회는 비었는데 넣으려니 중복."""
    repo.upsert_notice_version(session, record())
    real = repo._find_notice
    calls = []

    def stale_once(s, source_id, external_id):
        calls.append(1)
        return None if len(calls) == 1 else real(s, source_id, external_id)

    monkeypatch.setattr(repo, "_find_notice", stale_once)
    notice, version = repo.upsert_notice_version(session, record(deadline="2026-11-07"))
    assert version is not None and version.version_no == 2
    assert session.scalar(sa.select(sa.func.count()).select_from(models.Notice)) == 1


def test_upsert_retries_on_version_number_conflict(session, monkeypatch):
    """다른 워커가 같은 version_no를 먼저 썼다: 최신 버전을 다시 읽고 다음 번호로 쓴다."""
    repo.upsert_notice_version(session, record())
    repo.upsert_notice_version(session, record(deadline="2026-11-07"))
    real = repo._latest_version
    calls = []

    def stale_once(s, notice_id):
        calls.append(1)
        latest = real(s, notice_id)
        if len(calls) == 1:  # v2가 이미 있는데 v1을 최신으로 본다
            return s.scalar(
                sa.select(models.NoticeVersion).where(
                    models.NoticeVersion.notice_id == notice_id,
                    models.NoticeVersion.version_no == 1,
                )
            )
        return latest

    monkeypatch.setattr(repo, "_latest_version", stale_once)
    notice, version = repo.upsert_notice_version(session, record(deadline="2026-11-14"))
    assert version.version_no == 3 and notice.current_version_no == 3
    assert len(calls) == 2
