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


class Notice(Base):
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
    mime: Mapped[str | None] = mapped_column(sa.String(100))
    blob_key: Mapped[str | None] = mapped_column(sa.String(64))  # sha256
    parse_status: Mapped[str] = mapped_column(
        sa.String(16), default="pending"
    )  # pending/ok/needs_review


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


class FieldValueRow(Base):
    __tablename__ = "field_value"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    version_id: Mapped[str] = mapped_column(sa.ForeignKey("notice_version.id"))
    name: Mapped[str] = mapped_column(sa.String(64))
    value: Mapped[object] = mapped_column(JSONType)
    value_type: Mapped[str] = mapped_column(sa.String(16))
    source_lineage_ids: Mapped[list] = mapped_column(JSONType, default=list)
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0)
    extractor: Mapped[str] = mapped_column(sa.String(64), default="")


class EligibilityRuleRow(Base):
    __tablename__ = "eligibility_rule"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    version_id: Mapped[str] = mapped_column(sa.ForeignKey("notice_version.id"))
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
    __table_args__ = (sa.UniqueConstraint("user_id", "notice_id"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("profile.user_id"), index=True)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    fields: Mapped[list] = mapped_column(JSONType, default=list)


class JudgmentRow(Base):
    __tablename__ = "judgment"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("profile.user_id"), index=True)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    version_id: Mapped[str] = mapped_column(sa.ForeignKey("notice_version.id"))
    result: Mapped[str] = mapped_column(sa.String(16))  # eligible / ineligible / unknown
    reasons: Mapped[list] = mapped_column(JSONType, default=list)


class ChangeEvent(Base):
    __tablename__ = "change_event"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    from_version_id: Mapped[str | None] = mapped_column(sa.String(32))
    to_version_id: Mapped[str] = mapped_column(sa.String(32))
    changed_lineages: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class OutboxAlert(Base):
    __tablename__ = "outbox_alert"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), unique=True)
    user_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    notice_id: Mapped[str] = mapped_column(sa.String(32))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")  # pending / sent / failed
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class JobLedger(Base):
    __tablename__ = "job_ledger"
    stage: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    job_key: Mapped[str] = mapped_column(sa.String(200), primary_key=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="running")
    attempts: Mapped[int] = mapped_column(sa.Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now, onupdate=now
    )
