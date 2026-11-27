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


class Item(Base):
    """사업. 포털 게시물(notice) 여럿이 하나의 사업이 된다 (설계 6.1).

    기업마당은 정정할 때마다 새 번호로 다시 올리고 원공고를 닫는다. 구독·판정·알림은 게시물이 아니라
    사업에 걸어야 정정이 나와도 이어진다.
    """

    __tablename__ = "item"
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
    item_id: Mapped[str | None] = mapped_column(sa.ForeignKey("item.id"), index=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ItemLink(Base):
    """게시물 → 사업 연결의 근거. 점수가 낮으면 합치지 않고 운영자 확인으로 남긴다."""

    __tablename__ = "item_link"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(sa.ForeignKey("notice.id"), index=True)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
    method: Mapped[str] = mapped_column(sa.String(32))  # new / repost_title
    score: Mapped[float] = mapped_column(sa.Float, default=1.0)
    decision: Mapped[str] = mapped_column(sa.String(16))  # linked / needs_review
    candidate_notice_id: Mapped[str | None] = mapped_column(sa.String(32))
    decided_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)


class ItemVersion(Base):
    """엔진의 입력이 되는 사업 내용 버전. 게시물 버전의 본문 해시 + 첨부 내용 해시로 정한다."""

    __tablename__ = "item_version"
    __table_args__ = (
        sa.UniqueConstraint("item_id", "version_no"),
        sa.UniqueConstraint("item_id", "content_hash"),
    )
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
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
    # 이 실행에서 형식 변화로 보인 키: {출처: {키: {"kind", "changed", "compared"}}} (drift.py)
    drift: Mapped[dict | None] = mapped_column(JSONType)


class SourceDrift(Base):
    """출처의 형식 변화로 붙잡은 키 하나와 그 결정 (drift.py). 같은 키가 다시 붙잡히면 새 행이다."""

    __tablename__ = "source_drift"
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(sa.String(32), index=True)
    key: Mapped[str] = mapped_column(sa.String(100))
    kind: Mapped[str] = mapped_column(sa.String(16))  # emptied / filled / changed
    # held / ignored / released / recovered / closed
    status: Mapped[str] = mapped_column(sa.String(16), index=True)
    first_run: Mapped[str] = mapped_column(sa.String(64))
    last_run: Mapped[str] = mapped_column(sa.String(64))
    runs: Mapped[int] = mapped_column(sa.Integer, default=0)  # 붙잡힌 실행 수
    changed: Mapped[int] = mapped_column(
        sa.Integer, default=0
    )  # 마지막으로 잡힌 실행의 바뀐 항목 수
    compared: Mapped[int] = mapped_column(sa.Integer, default=0)
    samples: Mapped[list] = mapped_column(JSONType, default=list)  # 외부 ID 몇 개
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(sa.Text)


class ToolCallRow(Base):
    """에이전트의 도구 호출 하나 (usage.py). 값은 남기지 않는다: 인자는 모양과 해시, 결과는 크기와 개수."""

    __tablename__ = "tool_call"
    __table_args__ = (
        sa.Index("ix_tool_call_tool_at", "tool", "at"),
        sa.Index("ix_tool_call_session_seq", "session", "seq"),
    )
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    session: Mapped[str] = mapped_column(sa.String(32))  # 접속 하나 (MCP 서버 프로세스 하나)
    client: Mapped[str] = mapped_column(sa.String(64))
    seq: Mapped[int] = mapped_column(sa.Integer)  # 세션 안의 순서
    tool: Mapped[str] = mapped_column(sa.String(64))
    tool_version: Mapped[str] = mapped_column(sa.String(16))
    arg_shape: Mapped[dict] = mapped_column(JSONType, default=dict)  # {칸: "str:120"}
    arg_digest: Mapped[str] = mapped_column(sa.String(16))
    logged: Mapped[dict | None] = mapped_column(JSONType)  # 도구가 허락한 인자만, 가리고 자른 것
    status: Mapped[str] = mapped_column(sa.String(16))  # ok / error / exception
    error: Mapped[str | None] = mapped_column(sa.String(200))
    result_bytes: Mapped[int] = mapped_column(sa.Integer, default=0)
    result_count: Mapped[int | None] = mapped_column(sa.Integer)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, default=0)
    hidden_lines: Mapped[int] = mapped_column(sa.Integer, default=0)


class AgentFeedback(Base):
    """에이전트가 남긴 피드백 (usage.py). 신뢰할 수 없는 글이다: 자동으로 적용하지 않고 사람이 본다."""

    __tablename__ = "agent_feedback"
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    session: Mapped[str] = mapped_column(sa.String(32))
    client: Mapped[str] = mapped_column(sa.String(64))
    tool: Mapped[str | None] = mapped_column(sa.String(64))
    kind: Mapped[str] = mapped_column(
        sa.String(16)
    )  # wrong / lacking / confusing / slow / missing_tool / other
    message: Mapped[str] = mapped_column(sa.Text)  # 개인정보를 가리고 자른 것
    flagged_lines: Mapped[int] = mapped_column(sa.Integer, default=0)  # 지시로 보이는 줄 수
    status: Mapped[str] = mapped_column(
        sa.String(16), default="new"
    )  # new / reviewed / acted / dismissed
    note: Mapped[str | None] = mapped_column(sa.Text)


class ParseCache(Base):
    """첨부 파싱 결과. 같은 파일(내용 해시)은 한 번만 파싱한다. 파서를 바꾸면 이 표를 비운다."""

    __tablename__ = "parse_cache"
    blob_key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    mime: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    parse_status: Mapped[str] = mapped_column(sa.String(16))  # ok / needs_review
    parser: Mapped[str | None] = mapped_column(sa.String(64))
    parsed_key: Mapped[str | None] = mapped_column(sa.String(64))
    review_reason: Mapped[str | None] = mapped_column(sa.Text)
    segments: Mapped[int] = mapped_column(sa.Integer, default=0)


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


# 아래 테이블은 모두 항목(item, 지원사업 앱에서는 사업)과 항목 버전에 건다 (DESIGN 6.1). 기업마당이 정정 때 새 번호로
# 다시 올려도 구독·판정·알림이 끊기지 않게 하기 위해서다.


class AppDefinitionRow(Base):
    """앱 정의의 원본 (app-spec 9·15장). 판마다 한 줄이고, 켠 판은 내용을 바꾸지 않는다.

    우리 앱은 저장소의 파일(apps/*.json)을 씨앗으로 넣는다(app_registry.register).
    """

    __tablename__ = "app_definition"
    app_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    version: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    body: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    enabled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class FieldValueRow(Base):
    __tablename__ = "field_value"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
    item_version_id: Mapped[str] = mapped_column(sa.ForeignKey("item_version.id"))
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
    # 어느 앱의 몇 판으로 계산했나(app-spec 9·15장). 앱 정의는 app_definition에 있다
    app_id: Mapped[str] = mapped_column(sa.String(64))
    app_version: Mapped[int] = mapped_column(sa.Integer)


class EligibilityRuleRow(Base):
    __tablename__ = "eligibility_rule"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
    item_version_id: Mapped[str] = mapped_column(sa.ForeignKey("item_version.id"))
    group_id: Mapped[str] = mapped_column(sa.String(32))
    attribute: Mapped[str] = mapped_column(sa.String(64))
    op: Mapped[str] = mapped_column(sa.String(8))
    value: Mapped[object] = mapped_column(JSONType)
    source_lineage_id: Mapped[str | None] = mapped_column(sa.String(32))
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0)


class Person(Base):
    """사람 (platform-services 1.1, 4.2). id는 가명이다. 봉투·기록·판정은 이 id만 쓴다.

    본인이 지우면 값과 링크를 지우고 deleted_at만 남긴다(4.6).
    """

    __tablename__ = "person"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class AccessLink(Base):
    """사람에게 들어오는 길. 개인 링크의 토큰은 비밀이라 원문을 두지 않고 해시만 둔다."""

    __tablename__ = "access_link"
    token_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)  # sha256 hex
    person_id: Mapped[str] = mapped_column(sa.ForeignKey("person.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Profile(Base):
    """사람의 동의 기록(문구 판·시각·선택 동의). 값은 금고(vault_value)에 있다(platform-services 4.7).

    person_id는 person.id다. 구독·판정은 프로필이 아니라 사람을 가리킨다(마이그레이션 0019).
    """

    __tablename__ = "profile"
    person_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)
    # 개인정보 수집·이용 동의 (web/consent.py). 문구 판과 시각을 남긴다. research_ok는 선택 동의
    consent_version: Mapped[str | None] = mapped_column(sa.String(32))
    consented_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    research_ok: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())


class SubscriptionRow(Base):
    __tablename__ = "subscription"
    __table_args__ = (sa.UniqueConstraint("person_id", "item_id"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(sa.ForeignKey("person.id"), index=True)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
    fields: Mapped[list] = mapped_column(JSONType, default=list)
    # 어느 앱의 몇 판으로 계산했나(app-spec 9·15장). 앱 정의는 app_definition에 있다
    app_id: Mapped[str] = mapped_column(sa.String(64))
    app_version: Mapped[int] = mapped_column(sa.Integer)


class JudgmentRow(Base):
    """판정 이력. 덮어쓰지 않고 쌓는다(사용자·사업·사업 버전·규칙 해시마다 하나)."""

    __tablename__ = "judgment"
    __table_args__ = (
        sa.UniqueConstraint("person_id", "item_id", "item_version_id", "ruleset_hash"),
    )
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(sa.ForeignKey("person.id"), index=True)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
    item_version_id: Mapped[str] = mapped_column(sa.ForeignKey("item_version.id"))
    ruleset_hash: Mapped[str] = mapped_column(sa.String(64))
    result: Mapped[str] = mapped_column(sa.String(16))  # eligible / ineligible / unknown
    reasons: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    # 어느 앱의 몇 판으로 계산했나(app-spec 9·15장). 앱 정의는 app_definition에 있다
    app_id: Mapped[str] = mapped_column(sa.String(64))
    app_version: Mapped[int] = mapped_column(sa.Integer)


class ChangeEvent(Base):
    __tablename__ = "change_event"
    __table_args__ = (sa.UniqueConstraint("item_id", "to_version_id"),)
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
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
    item_id: Mapped[str] = mapped_column(sa.ForeignKey("item.id"), index=True)
    to_version_id: Mapped[str] = mapped_column(sa.ForeignKey("item_version.id"), index=True)
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
    # 어느 앱의 몇 판으로 계산했나(app-spec 9·15장). 앱 정의는 app_definition에 있다
    app_id: Mapped[str] = mapped_column(sa.String(64))
    app_version: Mapped[int] = mapped_column(sa.Integer)


class OutboxAlert(Base):
    __tablename__ = "outbox_alert"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), unique=True)
    person_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    item_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    # pending → held | ready → sending → sent, 그리고 superseded / discarded / corrected (DESIGN 7.8)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    held_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # 어느 앱의 몇 판으로 계산했나(app-spec 9·15장). 앱 정의는 app_definition에 있다
    app_id: Mapped[str] = mapped_column(sa.String(64))
    app_version: Mapped[int] = mapped_column(sa.Integer)


class EventRow(Base):
    """이벤트 봉투 (platform-services 2장, events.py). 추가만 한다."""

    __tablename__ = "event"
    __table_args__ = (sa.UniqueConstraint("type", "idempotency_key"),)
    id: Mapped[str] = mapped_column(sa.String(26), primary_key=True)  # ULID: 시간 순으로 정렬된다
    type: Mapped[str] = mapped_column(sa.String(64), index=True)
    v: Mapped[int] = mapped_column(sa.Integer)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    producer: Mapped[str] = mapped_column(sa.String(64))
    app: Mapped[str | None] = mapped_column(sa.String(80))
    subject: Mapped[str | None] = mapped_column(sa.String(64))
    item: Mapped[str | None] = mapped_column(sa.String(40), index=True)
    item_version: Mapped[int | None] = mapped_column(sa.Integer)
    trace_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    causation_id: Mapped[str | None] = mapped_column(sa.String(26))
    idempotency_key: Mapped[str] = mapped_column(sa.String(300))
    labels: Mapped[dict] = mapped_column(JSONType, default=dict)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)


class VaultValueRow(Base):
    """금고의 속성 값 (platform-services 4.2). 사람 단위로 한 번 두고, 값은 애플리케이션 단에서 암호화한다."""

    __tablename__ = "vault_value"
    person_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.String(16))  # input / derived / document
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ConsentRow(Base):
    """동의: 사람이 어느 앱에 어느 용도로 어느 키를 쓰게 했나. 철회해도 행은 남긴다(언제 무엇을)."""

    __tablename__ = "consent"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    app_id: Mapped[str] = mapped_column(sa.String(64))
    purpose: Mapped[str] = mapped_column(sa.String(16))  # judge / notify / assist / research
    keys: Mapped[list] = mapped_column(JSONType, default=list)
    text_version: Mapped[str | None] = mapped_column(sa.String(32))
    granted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class VaultGrantRow(Base):
    """허가: 동의 범위 안에서 줄인 키 목록을 가진 쪽(세션·플랫폼 부품)에 잠깐 준다(4.4)."""

    __tablename__ = "vault_grant"
    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    app_id: Mapped[str] = mapped_column(sa.String(64))
    purpose: Mapped[str] = mapped_column(sa.String(16))
    keys: Mapped[list] = mapped_column(JSONType, default=list)
    holder: Mapped[str] = mapped_column(sa.String(64))  # svc:engine, session:<id> …
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    uses_left: Mapped[int | None] = mapped_column(sa.Integer)  # None이면 만료까지
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class VaultAccessRow(Base):
    """금고에서 꺼낸 기록: 누가, 어느 용도로, 어떤 키를(값은 아님)."""

    __tablename__ = "vault_access"
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    grant_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    person_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    app_id: Mapped[str] = mapped_column(sa.String(64))
    purpose: Mapped[str] = mapped_column(sa.String(16))
    keys: Mapped[list] = mapped_column(JSONType, default=list)
    holder: Mapped[str] = mapped_column(sa.String(64))
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class TraceRow(Base):
    """비행 기록기의 묶음 하나 (platform-services 3.3, flight.py). 수집 실행 하나가 묶음 하나다.

    봉투(event)는 베끼지 않는다. 같은 trace_id로 엮인다. digest는 묶음을 닫을 때 낸 출력의 해시로,
    재생 결과와 비교한다(3.4 같게 재생).
    """

    __tablename__ = "trace"
    id: Mapped[str] = mapped_column(sa.String(26), primary_key=True)  # 첫 봉투의 id와 같다
    kind: Mapped[str] = mapped_column(sa.String(32))  # collect_run / eval / backtest / ...
    name: Mapped[str | None] = mapped_column(sa.String(100))  # 수집 실행 이름 등
    parent_id: Mapped[str | None] = mapped_column(sa.String(26))
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    platform_rev: Mapped[str] = mapped_column(sa.String(64))
    app: Mapped[str | None] = mapped_column(sa.String(80))
    config_hash: Mapped[str] = mapped_column(sa.String(64))
    config: Mapped[dict] = mapped_column(JSONType, default=dict)  # 해시를 낸 설정(판들)
    status: Mapped[str] = mapped_column(sa.String(16), default="open")  # open / closed
    digest: Mapped[str | None] = mapped_column(sa.String(64))
    events: Mapped[int | None] = mapped_column(sa.Integer)  # 닫을 때 센 봉투 수


class RecordRow(Base):
    """묶음 안에서 결정적이지 않은 경계를 지난 것 하나 (3.2). 큰 내용은 녹화본·원문 저장소에 두고 키만."""

    __tablename__ = "record"
    __table_args__ = (sa.UniqueConstraint("trace_id", "seq"),)
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(sa.String(26), index=True)
    seq: Mapped[int] = mapped_column(sa.Integer)
    kind: Mapped[str] = mapped_column(sa.String(16))  # parse / llm_call / tool_call / decision
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ref: Mapped[str] = mapped_column(sa.String(200), index=True)  # "cassette:<키>", "blob:<키>"
    digest: Mapped[str | None] = mapped_column(sa.String(64))  # 출력의 해시
    cost: Mapped[int | None] = mapped_column(sa.Integer)  # 토큰 등
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)  # 작게, 가린 것만


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


class AgentSessionRow(Base):
    """에이전트 세션 (platform-services 5.3). 사람·앱·에이전트 단위로 이어진다.

    켜진 속성(flags: A·B·C)은 켜지기만 하고, 다시 접속해도 초기화되지 않는다. 닫는 것은 사람(운영자)이다.
    """

    __tablename__ = "agent_session"
    __table_args__ = (sa.Index("ix_agent_session_owner", "person_id", "app_id", "agent", "status"),)
    id: Mapped[str] = mapped_column(sa.String(26), primary_key=True)
    person_id: Mapped[str | None] = mapped_column(sa.String(32))
    app_id: Mapped[str] = mapped_column(sa.String(64))
    agent: Mapped[str] = mapped_column(sa.String(64))  # 접속한 에이전트(클라이언트) 이름
    flags: Mapped[list] = mapped_column(JSONType, default=list)
    seen: Mapped[list] = mapped_column(JSONType, default=list)  # 외부 글(A)에서 본 주소·전화의 해시
    sources: Mapped[list] = mapped_column(
        JSONType, default=list
    )  # 본 외부 글(도구·참조), 최근 것만
    calls: Mapped[dict] = mapped_column(JSONType, default=dict)  # 도구별 호출 수
    effects: Mapped[int] = mapped_column(sa.Integer, default=0)  # 요청한 외부 효과 수
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(sa.String(16), default="open")  # open / closed
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ToolActionRow(Base):
    """효과 하나 (5.5). 밖으로 나가는 효과는 바로 실행하지 않고 여기에 둔다.

    held(사람의 승인을 기다림) → approved(보류 시간을 기다림, 이때도 취소할 수 있다) → committed,
    또는 discarded · expired · blocked · failed. 플랫폼 안의 되돌릴 수 있는 효과(internal)는
    바로 committed이고 되돌리는 정보(undo)를 둔다.
    """

    __tablename__ = "tool_action"
    id: Mapped[str] = mapped_column(sa.String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.String(26), index=True)
    tool: Mapped[str] = mapped_column(sa.String(64))
    effect: Mapped[str] = mapped_column(sa.String(16))  # internal / external
    arguments: Mapped[dict] = mapped_column(JSONType, default=dict)
    dropped: Mapped[list] = mapped_column(JSONType, default=list)  # split으로 뺀 인자 이름
    status: Mapped[str] = mapped_column(sa.String(16), index=True)
    policy: Mapped[str] = mapped_column(sa.String(16))  # pass / approve / split / block
    marks: Mapped[list] = mapped_column(JSONType, default=list)  # 받는 쪽 표시
    reason: Mapped[str | None] = mapped_column(sa.String(300))
    evidence: Mapped[list] = mapped_column(JSONType, default=list)  # 이 세션이 본 외부 글
    requested_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    decide_by: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(sa.String(64))  # 마지막 결정(승인·거절·취소)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(sa.String(64))  # 사람 또는 policy:pass·split
    commit_after: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONType)
    undo: Mapped[dict | None] = mapped_column(JSONType)
    undone_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ToolDecisionRow(Base):
    """게이트웨이의 판단 하나 (5.4의 5번 기록). 인자 값은 남기지 않는다."""

    __tablename__ = "tool_decision"
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)  # 순서
    session_id: Mapped[str] = mapped_column(sa.String(26), index=True)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    tool: Mapped[str] = mapped_column(sa.String(64))
    decision: Mapped[str] = mapped_column(sa.String(16))  # pass / held / approved / block / error
    step: Mapped[str] = mapped_column(
        sa.String(16)
    )  # declare / consent / transition / target / run
    reason: Mapped[str | None] = mapped_column(sa.String(300))
    flags_before: Mapped[list] = mapped_column(JSONType, default=list)
    flags_after: Mapped[list] = mapped_column(JSONType, default=list)
    action_id: Mapped[str | None] = mapped_column(sa.String(26))


class AppMemberRow(Base):
    """앱의 사람과 역할 (app-spec 6장): 소유자(owner)는 정의를 고치고 켜고 예산을 정한다.

    구독자는 동의·구독으로 드러나므로 따로 적지 않는다. 지원사업 앱처럼 운영자가 저장소에서 넣은 앱은
    소유자 행이 없다(에이전트가 고칠 수 없다).
    """

    __tablename__ = "app_member"
    app_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    person_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    role: Mapped[str] = mapped_column(sa.String(16), primary_key=True)  # owner / manager
    added_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class AppBudgetRow(Base):
    """앱별 하루 LLM 토큰 한도 (app-spec 9장). 소유자가 정하고, 올릴 때는 사람의 확인을 받는다.

    행이 없으면 기본값(주 앱은 워커의 하루 한도, 다른 앱은 app_registry.DEFAULT_DAILY_TOKENS)을 쓴다.
    """

    __tablename__ = "app_budget"
    app_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    daily_tokens: Mapped[int] = mapped_column(sa.Integer)
    set_by: Mapped[str] = mapped_column(sa.String(64))
    set_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class UserSourceRow(Base):
    """사용자가 등록한 출처 한 장 (source-catalog.md 4장). 나만 쓰는 출처(private)로 시작한다.

    등록은 이용 조건 동의가 필요한 순간이라 도구 게이트웨이의 확인을 거친다. 소유자의 앱만 쓴다.
    """

    __tablename__ = "user_source"
    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)  # 출처 id(source.id와 같다)
    owner: Mapped[str] = mapped_column(sa.String(32), index=True)  # person id
    body: Mapped[dict] = mapped_column(JSONType)  # 출처 한 장(catalog.SourceSheet)
    registered_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
