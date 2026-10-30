"""설계 6장 데이터 모델 (SQLAlchemy 2.0).

임베딩 컬럼(pgvector)은 PostgreSQL 전용이라 큐·검색 단계(5주차 이후)에서 마이그레이션으로 더한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "source"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)  # kstartup / bizinfo
    name: Mapped[str] = mapped_column(sa.String(100))
    base_url: Mapped[str | None] = mapped_column(sa.String(500))
    rate_limit_per_sec: Mapped[float | None] = mapped_column(sa.Float)


class Program(Base):
    """사업. 포털 게시물(notice) 여럿이 하나의 사업이 된다 (설계 6.1).

    기업마당은 정정할 때마다 새 번호로 다시 올리고 원공고를 닫는다. 구독·판정·알림은 게시물이 아니라
    사업에 걸어야 정정이 나와도 이어진다.
    """

    __tablename__ = "program"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(sa.Text)
    title_key: Mapped[str] = mapped_column(sa.String(300), index=True)  # 정정 표시를 뺀 정규화 제목
    org: Mapped[str | None] = mapped_column(sa.String(200))
    current_version_no: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class Notice(Base):
    """포털 게시물(posting). 출처와 외부 ID 하나."""

    __tablename__ = "notice"
    __table_args__ = (sa.UniqueConstraint("source_id", "external_id"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"))
    external_id: Mapped[str] = mapped_column(sa.String(100))
    title: Mapped[str] = mapped_column(sa.Text)
    org: Mapped[str | None] = mapped_column(sa.String(200))
    url: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(16), default="open")  # open / closed
    current_version_id: Mapped[str | None] = mapped_column(sa.String(32))
    current_version_no: Mapped[int | None] = mapped_column(sa.Integer)  # 순서 보장용 (설계 8장)
    program_id: Mapped[str | None] = mapped_column(sa.ForeignKey("program.id"), index=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ProgramLink(Base):
    """게시물 → 사업 연결의 근거. 점수가 낮으면 합치지 않고 운영자 확인으로 남긴다."""

    __tablename__ = "program_link"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    method: Mapped[str] = mapped_column(sa.String(32))  # new / repost_title
    score: Mapped[float] = mapped_column(sa.Float, default=1.0)
    decision: Mapped[str] = mapped_column(sa.String(16))  # linked / needs_review
    candidate_notice_id: Mapped[str | None] = mapped_column(sa.String(32))
    decided_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class ProgramVersion(Base):
    """엔진의 입력이 되는 사업 내용 버전. 게시물 버전의 본문 해시 + 첨부 내용 해시로 정한다."""

    __tablename__ = "program_version"
    __table_args__ = (
        sa.UniqueConstraint("program_id", "version_no"),
        sa.UniqueConstraint("program_id", "content_hash"),
    )
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    version_no: Mapped[int] = mapped_column(sa.Integer)
    notice_version_id: Mapped[str] = mapped_column(sa.ForeignKey("notice_version.id"))
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    source_time: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class SnapshotRecord(Base):
    """상시 수집이 본 (출처, 외부 ID, 내용 해시) 하나. 같은 내용은 처음·마지막으로 본 시각만 고친다."""

    __tablename__ = "snapshot"
    __table_args__ = (sa.UniqueConstraint("source", "external_id", "content_hash"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(sa.String(32))
    external_id: Mapped[str] = mapped_column(sa.String(100))
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    item_key: Mapped[str] = mapped_column(sa.String(64))  # 원문 저장소의 정규 JSON
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class LoadedRun(Base):
    __tablename__ = "snapshot_run"
    name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)  # runs/<이름>.json
    loaded_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class NoticeVersion(Base):
    __tablename__ = "notice_version"
    __table_args__ = (sa.UniqueConstraint("notice_id", "version_no"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    version_no: Mapped[int] = mapped_column(sa.Integer)
    kind: Mapped[str] = mapped_column(sa.String(16))  # original / correction
    fetched_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    body: Mapped[str] = mapped_column(sa.Text, default="")
    raw: Mapped[dict] = mapped_column(JSONType, default=dict)


class Attachment(Base):
    __tablename__ = "attachment"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_version_id: Mapped[str] = mapped_column(sa.ForeignKey("notice_version.id"), index=True)
    filename: Mapped[str] = mapped_column(sa.Text)
    url: Mapped[str] = mapped_column(sa.Text)
    role: Mapped[str] = mapped_column(sa.String(16), default="attachment")  # notice / attachment
    # 버전 사이의 같은 첨부: 역할 + 정정 표시를 뺀 파일 이름 + 같은 이름 안의 순번
    lineage_key: Mapped[str | None] = mapped_column(sa.String(300))
    mime: Mapped[str | None] = mapped_column(sa.String(100))
    blob_key: Mapped[str | None] = mapped_column(sa.String(64))  # sha256
    parse_status: Mapped[str] = mapped_column(
        sa.String(16), default="pending"
    )  # pending / ok / needs_review / unsupported
    parser: Mapped[str | None] = mapped_column(sa.String(64))  # 예: kordoc 4.18.13
    parsed_key: Mapped[str | None] = mapped_column(sa.String(64))  # 파서 출력 JSON의 원문 저장소 키
    review_reason: Mapped[str | None] = mapped_column(sa.Text)


class SegmentRow(Base):
    __tablename__ = "segment"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_version_id: Mapped[str] = mapped_column(sa.ForeignKey("notice_version.id"), index=True)
    lineage_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16))
    ord: Mapped[int] = mapped_column(sa.Integer)
    text: Mapped[str] = mapped_column(sa.Text)
    text_hash: Mapped[str] = mapped_column(sa.String(64))
    locator: Mapped[dict] = mapped_column(JSONType, default=dict)


# 아래 테이블은 모두 사업(program)과 사업 버전에 건다 (DESIGN 6.1). 기업마당이 정정 때 새 번호로
# 다시 올려도 구독·판정·알림이 끊기지 않게 하기 위해서다.


class FieldValueRow(Base):
    __tablename__ = "field_value"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    program_version_id: Mapped[str] = mapped_column(sa.ForeignKey("program_version.id"))
    name: Mapped[str] = mapped_column(sa.String(64))
    value: Mapped[object] = mapped_column(JSONType)
    value_type: Mapped[str] = mapped_column(sa.String(16))
    source_lineage_ids: Mapped[list] = mapped_column(JSONType, default=list)
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0)
    status: Mapped[str] = mapped_column(
        sa.String(16), default="verified"
    )  # verified/needs_review/stale
    extractor: Mapped[str] = mapped_column(sa.String(64), default="")
    template_version: Mapped[str | None] = mapped_column(sa.String(32))  # 추출 프롬프트 버전
    llm_call_key: Mapped[str | None] = mapped_column(sa.String(64))  # 녹화본 키


class EligibilityRuleRow(Base):
    __tablename__ = "eligibility_rule"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    program_version_id: Mapped[str] = mapped_column(sa.ForeignKey("program_version.id"))
    group_id: Mapped[str] = mapped_column(sa.String(32))
    attribute: Mapped[str] = mapped_column(sa.String(64))
    op: Mapped[str] = mapped_column(sa.String(8))
    value: Mapped[object] = mapped_column(JSONType)
    source_lineage_id: Mapped[str | None] = mapped_column(sa.String(32))
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0)


class Profile(Base):
    __tablename__ = "profile"
    user_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)


class SubscriptionRow(Base):
    __tablename__ = "subscription"
    __table_args__ = (sa.UniqueConstraint("user_id", "program_id"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("profile.user_id"), index=True)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    fields: Mapped[list] = mapped_column(JSONType, default=list)


class JudgmentRow(Base):
    """판정 이력. 덮어쓰지 않고 쌓는다(사용자·사업·사업 버전·규칙 해시마다 하나)."""

    __tablename__ = "judgment"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "program_id", "program_version_id", "ruleset_hash"),
    )
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("profile.user_id"), index=True)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    program_version_id: Mapped[str] = mapped_column(sa.ForeignKey("program_version.id"))
    ruleset_hash: Mapped[str] = mapped_column(sa.String(64))
    result: Mapped[str] = mapped_column(sa.String(16))  # eligible / ineligible / unknown
    reasons: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class ChangeEvent(Base):
    __tablename__ = "change_event"
    __table_args__ = (sa.UniqueConstraint("program_id", "to_version_id"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    from_version_id: Mapped[str | None] = mapped_column(sa.String(32))
    to_version_id: Mapped[str] = mapped_column(sa.String(32))
    changed_lineages: Mapped[dict] = mapped_column(JSONType, default=dict)
    matcher_version: Mapped[str] = mapped_column(sa.String(32), default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class LabelRow(Base):
    """사람이 단 정답 라벨 (DESIGN 12.1). 단위는 (사업 버전 쌍, 필드), 회차마다 따로 쌓는다.

    엔진과 무관하게 to_version(이후 사업 버전)으로 가리킨다. 원문은 넣지 않고 위치만 적는다.
    """

    __tablename__ = "label"
    __table_args__ = (sa.UniqueConstraint("to_version_id", "field", "round", "labeler"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(sa.ForeignKey("program.id"), index=True)
    to_version_id: Mapped[str] = mapped_column(sa.ForeignKey("program_version.id"), index=True)
    field: Mapped[str] = mapped_column(sa.String(64))
    round: Mapped[int] = mapped_column(sa.Integer, default=1)  # 1: 처음, 2: 7일 뒤 다시
    labeler: Mapped[str] = mapped_column(sa.String(64), default="me")
    # changed / unchanged / wording_only / absent
    status: Mapped[str] = mapped_column(sa.String(16))
    before: Mapped[str | None] = mapped_column(sa.Text)  # 정규화한 값 (지침 문서)
    after: Mapped[str | None] = mapped_column(sa.Text)
    visible_in: Mapped[str | None] = mapped_column(sa.String(16))  # api_meta / body / attachment
    evidence: Mapped[str | None] = mapped_column(sa.Text)  # 위치만: "공고문.hwp 2쪽 표1 신청자격"
    note: Mapped[str | None] = mapped_column(sa.Text)
    labeled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class OutboxAlert(Base):
    __tablename__ = "outbox_alert"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), unique=True)
    user_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    program_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    # pending → held | ready → sending → sent, 그리고 superseded / discarded / corrected (DESIGN 7.8)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    held_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Job(Base):
    """PostgreSQL 작업 큐 (설계 8장). 단계 결과 기록과 다음 작업 등록을 한 트랜잭션으로 묶는다."""

    __tablename__ = "job"
    __table_args__ = (
        sa.Index("ix_job_ready", "status", "run_after", "priority"),
        sa.Index("ix_job_concurrency", "concurrency_key", "status"),
    )
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(sa.String(32))
    dedupe_key: Mapped[str] = mapped_column(sa.String(300), unique=True)
    concurrency_key: Mapped[str | None] = mapped_column(sa.String(100))  # 같은 키는 하나씩만
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    priority: Mapped[int] = mapped_column(sa.Integer, default=100)  # 작을수록 먼저
    # queued / running / retry / done / dead
    status: Mapped[str] = mapped_column(sa.String(16), default="queued")
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(sa.String(64))
    error_class: Mapped[str | None] = mapped_column(sa.String(16))  # transient / permanent
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )  # 마지막 꺼낸 시각
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now, onupdate=now
    )
