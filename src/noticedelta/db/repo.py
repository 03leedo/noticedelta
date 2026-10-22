"""저장소 함수: 버전 기록, 순서 보장, 멱등 처리 (설계 8장)."""

from __future__ import annotations

from dataclasses import asdict

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noticedelta.db.models import (
    Attachment,
    JobLedger,
    Notice,
    NoticeVersion,
    OutboxAlert,
    Source,
    new_id,
)
from noticedelta.impact import Alert
from noticedelta.matching import stable_filename
from noticedelta.sources import NoticeRecord


def _ensure_source(session: Session, source_id: str) -> None:
    if session.get(Source, source_id) is None:
        session.add(Source(id=source_id, name=source_id))
        session.flush()


def advance_current_version(
    session: Session, notice_id: str, version_id: str, version_no: int
) -> bool:
    """version_no가 지금보다 클 때만 현재 버전을 바꾼다. 늦게 도착한 옛 버전은 무시된다."""
    stmt = (
        sa.update(Notice)
        .where(Notice.id == notice_id)
        .where(sa.or_(Notice.current_version_no.is_(None), Notice.current_version_no < version_no))
        .values(current_version_id=version_id, current_version_no=version_no)
        .execution_options(synchronize_session="fetch")
    )
    return session.execute(stmt).rowcount == 1


def _find_notice(session: Session, source_id: str, external_id: str) -> Notice | None:
    return session.scalar(
        sa.select(Notice).where(Notice.source_id == source_id, Notice.external_id == external_id)
    )


def _latest_version(session: Session, notice_id: str) -> NoticeVersion | None:
    return session.scalar(
        sa.select(NoticeVersion)
        .where(NoticeVersion.notice_id == notice_id)
        .order_by(NoticeVersion.version_no.desc())
        .limit(1)
    )


def latest_version(session: Session, notice_id: str) -> NoticeVersion | None:
    return _latest_version(session, notice_id)


def _get_or_create_notice(session: Session, rec: NoticeRecord) -> Notice:
    notice = _find_notice(session, rec.source, rec.external_id)
    if notice is not None:
        return notice
    try:
        with session.begin_nested():
            notice = Notice(
                source_id=rec.source,
                external_id=rec.external_id,
                title=rec.title,
                org=rec.org,
                url=rec.url,
            )
            session.add(notice)
    except IntegrityError:
        # 다른 워커가 같은 공고를 먼저 만들었다. 그쪽 행을 쓴다.
        notice = _find_notice(session, rec.source, rec.external_id)
        if notice is None:
            raise
    return notice


def upsert_notice_version(
    session: Session, rec: NoticeRecord, *, max_attempts: int = 3
) -> tuple[Notice, NoticeVersion | None]:
    """수집 결과를 기록한다. 내용 해시가 직전 버전과 같으면 새 버전을 만들지 않는다.

    두 워커가 같은 공고를 동시에 기록해 버전 번호가 부딪히면, 최신 버전을 다시 읽고 다시 판단한다.
    """
    _ensure_source(session, rec.source)
    notice = _get_or_create_notice(session, rec)

    for attempt in range(max_attempts):
        latest = _latest_version(session, notice.id)
        if latest is not None and latest.content_hash == rec.content_hash:
            return notice, None
        seen_before = session.scalar(
            sa.select(NoticeVersion.id)
            .where(
                NoticeVersion.notice_id == notice.id,
                NoticeVersion.content_hash == rec.content_hash,
            )
            .limit(1)
        )
        if seen_before is not None:
            return notice, None  # 늦게 도착한 옛 스냅샷. 새 버전으로 올리지 않는다.

        version = NoticeVersion(
            id=new_id(),
            notice_id=notice.id,
            version_no=(latest.version_no + 1) if latest else 1,
            kind="correction" if latest else "original",
            content_hash=rec.content_hash,
            body=rec.body,
            raw=rec.raw,
        )
        try:
            with session.begin_nested():
                session.add(version)
                session.flush()  # 관계가 선언돼 있지 않아 버전을 먼저 넣어야 첨부의 외래 키가 맞는다
                seen: dict[str, int] = {}
                for ref in rec.attachments:
                    base = f"{ref.role}:{stable_filename(ref.filename)}"
                    seen[base] = seen.get(base, -1) + 1
                    session.add(
                        Attachment(
                            notice_version_id=version.id,
                            filename=ref.filename,
                            url=ref.url,
                            role=ref.role,
                            lineage_key=f"{base}:{seen[base]}",
                        )
                    )
                session.flush()
        except IntegrityError:
            if attempt == max_attempts - 1:
                raise
            continue
        break

    notice.title, notice.org, notice.url = rec.title, rec.org, rec.url
    session.flush()
    advance_current_version(session, notice.id, version.id, version.version_no)
    session.refresh(notice)
    return notice, version


def attachments_to_fetch(session: Session, version_id: str) -> list[Attachment]:
    """이 버전의 첨부 중 아직 원문 저장소에 없는 것."""
    return list(
        session.scalars(
            sa.select(Attachment)
            .where(Attachment.notice_version_id == version_id, Attachment.blob_key.is_(None))
            .order_by(Attachment.filename)
        )
    )


def enqueue_alert(session: Session, alert: Alert) -> bool:
    """아웃박스에 알림을 쓴다. 같은 멱등 키가 이미 있으면 False."""
    try:
        with session.begin_nested():
            session.add(
                OutboxAlert(
                    idempotency_key=alert.idempotency_key,
                    user_id=alert.user_id,
                    program_id=alert.program_id,
                    payload=asdict(alert),
                )
            )
    except IntegrityError:
        return False
    return True


def claim_job(session: Session, stage: str, job_key: str) -> bool:
    """(stage, job_key) 작업을 처음 맡을 때만 True. 끝난·진행 중 작업은 건너뛴다."""
    try:
        with session.begin_nested():
            session.add(JobLedger(stage=stage, job_key=job_key))
    except IntegrityError:
        return False
    return True
