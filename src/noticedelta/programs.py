"""상시 수집 스냅샷을 DB에 넣고 게시물을 사업(program)으로 묶는다 (설계 6.1, 작업 목록 10번).

실행 파일(runs/<시각>.json)을 시간 순으로 한 번씩만 읽는다.

1. 스냅샷 기록: (출처, 외부 ID, 내용 해시)마다 처음·마지막으로 본 시각.
2. 게시물 버전: 내용이 바뀐 공고만 notice_version을 만든다(repo.upsert_notice_version).
   첨부는 다시 받지 않고, 수집기가 이미 받은 원본 키를 그대로 쓴다.
3. 사업 연결: 새 게시물은 같은 기관의 기존 게시물 중 정정 표시를 뺀 제목이 같은 것과 잇는다
   (기업마당 재게시). 제목이 비슷하기만 하면 합치지 않고 needs_review로 남긴다.
   잘못 합치는 것이 놓치는 것보다 위험하다(남의 자격 조건이 내 사업으로 들어온다).
4. 사업 버전: 본문 해시(첨부 주소 제외, 표기 차이 무시) + 첨부 내용 해시가 바뀔 때만 새 버전. 번호는 실행 시간 순이다.
   주소만 새로 발급되고 파일이 같으면 게시물 버전만 생기고 사업 버전은 그대로다.
5. 이번 실행에 없는 게시물은 닫힌 것으로 표시한다(그 출처가 오류 없이 끝났을 때만).
6. 출처 형식 변화: 한 실행에서 많은 게시물의 같은 키가 한꺼번에 바뀌거나 비거나 채워지면 정정으로
   보지 않는다. 마지막으로 본 값을 이어 두고 운영자의 결정을 기다린다(drift.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta import drift, user_sources
from noticedelta.blobstore import BlobStore
from noticedelta.db import repo
from noticedelta.db.models import (
    Attachment,
    Item,
    ItemLink,
    ItemVersion,
    LoadedRun,
    Notice,
    NoticeVersion,
    SnapshotRecord,
)
from noticedelta.sources import NoticeRecord, bizinfo, kstartup
from noticedelta.sources.base import stable_url
from noticedelta.sources.bizinfo import html_to_text
from noticedelta.tool_gateway import reachable

TO_RECORD = {"kstartup": kstartup.to_record, "bizinfo": bizinfo.to_record}
DROP_PARAMS = {"bizinfo": bizinfo.DROP_PARAMS}  # 옛 실행 파일의 첨부 주소에서 뗄 값
AUTO_LINK = 1.0  # 정정 표시를 뺀 제목과 기관이 같을 때만 자동으로 잇는다
REVIEW_LINK = 0.8  # 그보다 낮지만 이 이상이면 운영자 확인으로 남긴다(같은 기관 안에서만)

# 정정 표시. "1차·2차"는 모집 회차라 지우지 않는다(다른 사업일 수 있다).
_REVISION = re.compile(r"수정|정정|변경|연장|재공고|재게시")
# 거의 모든 제목에 붙는 일반 단어. 정정 표시를 지울 때 "변경 공고"의 "공고"만 남는 식으로
# 키가 어긋나지 않게 아예 뺀다. 이 단어들로는 서로 다른 사업이 구별되지 않는다.
_GENERIC = re.compile(r"공고문?|안내|알림|게시")
_NOISE = re.compile(r"[\s\[\]()（）【】<>「」『』·ㆍ,._\-~∼:]+")


def title_key(title: str) -> str:
    """정정 표시와 괄호·공백을 뺀 제목."""
    t = _REVISION.sub("", title or "")
    return _NOISE.sub("", _GENERIC.sub("", t)).lower()


# 제목이 거의 같아도 이것이 다르면 다른 사업이다: [지역] 태그, 시·군·구 이름, 모집 회차.
_DISTINCT = re.compile(
    r"\[[^\]]{1,10}\]|[가-힣]{1,4}(?:특별자치도|광역시|특별시|시|군|구)(?=\s)|\d+\s*차"
)


def distinct_marks(title: str) -> set[str]:
    # "[수정공고]" 같은 정정 표시 괄호는 구별 표시가 아니다. 정정·일반 단어를 먼저 지운다.
    t = _GENERIC.sub("", _REVISION.sub("", title or ""))
    marks = {re.sub(r"\s", "", m) for m in _DISTINCT.findall(t)}
    return {m for m in marks if m.strip("[]")}


def _grams(s: str) -> set[str]:
    return {s[i : i + 3] for i in range(len(s) - 2)} or {s}


def title_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / len(ga | gb)


# 제목 앞의 상태 표시. 기간 값은 그대로 두고 제목만 [마감]으로 바꾸는 출처가 있다(K-Startup, 2026-11-25 라벨 12번)
CLOSED_MARK = re.compile(r"^\s*[\[\(【<]\s*(조기\s*)?마감\s*[\]\)】>]")
CLOSED = "마감"
TITLE_KEYS = ("pblancNm", "biz_pbanc_nm", "intg_pbanc_biz_nm")  # 출처마다 제목이 든 키


def closed_by_title(rec: NoticeRecord) -> bool:
    titles = [rec.title, *(str(rec.raw.get(k) or "") for k in TITLE_KEYS)]
    return any(CLOSED_MARK.search(t) for t in titles)


def source_meta(source: str, raw: dict) -> dict:
    """출처가 구조화해 주는 값을 카탈로그 이름으로(sources/catalog의 meta). 앱의 from: {meta: 이름}이 읽는다.

    제목에 [마감]이 붙으면 기간 값이 그대로여도 apply_end는 "마감"이다(조기 마감).
    """
    to_record = TO_RECORD.get(source)
    if to_record is None:
        return {}
    rec = to_record(raw)
    end = CLOSED if closed_by_title(rec) else rec.apply_end
    return {"apply_end": end, "apply_start": rec.apply_start}


def api_meta(source: str, raw: dict) -> dict:
    """출처 API가 구조화해 주는 값(접수 기간). 마감 연장 같은 변경은 이것만으로 보인다(평가의 B0).

    제목에 [마감]이 붙으면 기간 값이 그대로여도 마감은 "마감"이다(조기 마감).
    """
    to_record = TO_RECORD.get(source)
    if to_record is None:
        return {}
    rec = to_record(raw)
    deadline = CLOSED if closed_by_title(rec) else rec.apply_end
    return {"deadline": deadline, "apply_start": rec.apply_start}


# 출처 API의 문의처·신청 방법 칸. 기관이 API에 직접 넣는 값이라, 에이전트가 메일을 보낼 받는 쪽의 허용
# 목록으로 쓴다(도구 게이트웨이 5.4의 4번). 공고 본문·첨부의 글이나 LLM이 뽑은 값은 쓰지 않는다.
# 2026-11-26 dev.db: 기업마당 2,156건 중 문의처 칸만이면 849건(39%), 신청 방법 칸까지 1,288건(60%)에
# 메일 주소가 있다. K-Startup 482건은 문의처 칸(전화번호)만이면 0건, 이메일 접수 칸까지 118건(24%)
CONTACT_KEYS = {
    "bizinfo": ("refrncNm", "reqstMthPapersCn"),
    "kstartup": ("prch_cnpl_no", "aply_mthd_eml_rcpt_istc"),
}


def api_contacts(source: str, raw: dict) -> set[str]:
    """출처 API의 문의처 칸에 적힌 메일 주소·전화번호(정규화한 것)."""
    out: set[str] = set()
    for key in CONTACT_KEYS.get(source, ()):
        out |= reachable(html_to_text(str(raw.get(key) or "")))
    return out


def composite_hash(rec: NoticeRecord, blob_keys: list[str | None]) -> str:
    """사업 버전의 내용 해시: 첨부 주소를 뺀 본문 해시 + 첨부 내용 해시(받지 못한 첨부는 missing)."""
    parts = [rec.body_hash] + sorted(k or "missing" for k in blob_keys)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _when(value: str | None) -> datetime:
    return datetime.fromisoformat(value) if value else datetime.now()


@dataclass
class LoadSummary:
    runs: int = 0
    items: int = 0
    new_postings: int = 0
    new_posting_versions: int = 0
    new_items: int = 0
    new_item_versions: int = 0
    links: Counter = field(default_factory=Counter)
    closed: int = 0
    drift: dict = field(default_factory=dict)  # {실행 이름: {출처: {키: {"lost", "had"}}}}
    new_version_ids: list[str] = field(default_factory=list)  # 이번에 생긴 사업 버전
    refilled_version_ids: list[str] = field(default_factory=list)  # 첨부가 늦게 채워진 사업 버전


def _link_item(session: Session, notice: Notice, rec: NoticeRecord, summary: LoadSummary) -> None:
    key = title_key(rec.title)
    org = rec.org
    best, best_score = None, 0.0
    if org:
        candidates = session.scalars(
            sa.select(Notice).where(
                Notice.source_id == notice.source_id,
                Notice.org == org,
                Notice.id != notice.id,
                Notice.item_id.is_not(None),
            )
        ).all()
        marks = distinct_marks(rec.title)
        for cand in candidates:
            if distinct_marks(cand.title) != marks:
                continue  # 지역·시군구·회차가 다르면 비슷해도 다른 사업이다
            score = title_score(key, title_key(cand.title))
            if score > best_score:
                best, best_score = cand, score
    if best is not None and best_score >= AUTO_LINK:
        notice.item_id = best.item_id
        decision, method = "linked", "repost_title"
    else:
        program = Item(title=rec.title, title_key=key, org=org)
        session.add(program)
        session.flush()
        notice.item_id = program.id
        summary.new_items += 1
        decision, method = "linked", "new"
        if best is not None and best_score >= REVIEW_LINK:
            # 비슷하지만 같다고 확신할 수 없다. 따로 두고 확인 목록에 올린다.
            session.add(
                ItemLink(
                    notice_id=notice.id,
                    item_id=best.item_id,
                    method="repost_title",
                    score=round(best_score, 3),
                    decision="needs_review",
                    candidate_notice_id=best.id,
                )
            )
            summary.links["needs_review"] += 1
    session.add(
        ItemLink(
            notice_id=notice.id,
            item_id=notice.item_id,
            method=method,
            score=round(best_score, 3) if method != "new" else 1.0,
            decision=decision,
            candidate_notice_id=best.id if method != "new" and best else None,
        )
    )
    summary.links[method] += 1


def _attach_blobs(
    session: Session, version: NoticeVersion, item: dict, drop: tuple[str, ...] = ()
) -> tuple[list[str | None], int]:
    """수집기가 받은 첨부 원본 키를 첨부 행에 옮긴다 (lineage 키는 repo가 버전을 만들 때 붙인다).

    (첨부 키 목록, 이번에 새로 채운 수). 수집기는 실패한 첨부를 다음 실행에서 다시 받으므로,
    내용이 그대로인 게시물도 늦게 받은 첨부를 여기서 채운다.
    """
    # 2026-10-30~11-12 실행 파일에는 토큰이 붙은 주소가 남아 있다. 양쪽 다 토큰을 떼고 맞춘다.
    by_url = {stable_url(a["url"], drop): a for a in item.get("attachments", [])}
    rows = session.scalars(
        sa.select(Attachment).where(Attachment.notice_version_id == version.id)
    ).all()
    keys, filled = [], 0
    for att in rows:
        snap = by_url.get(stable_url(att.url, drop), {})
        if snap.get("blob_key") and att.blob_key is None:
            att.blob_key, att.mime = snap["blob_key"], snap.get("mime")
            filled += 1
        keys.append(att.blob_key)
    return keys, filled


def _item_version(
    session: Session, notice: Notice, version: NoticeVersion, chash: str, at: datetime
) -> ItemVersion | None:
    program = session.get(Item, notice.item_id)
    exists = session.scalar(
        sa.select(ItemVersion.id).where(
            ItemVersion.item_id == program.id, ItemVersion.content_hash == chash
        )
    )
    if exists:
        return None  # 늦게 온 옛 내용이거나 같은 내용의 재게시
    no = (program.current_version_no or 0) + 1
    pv = ItemVersion(
        item_id=program.id,
        version_no=no,
        notice_version_id=version.id,
        content_hash=chash,
        source_time=at,
    )
    session.add(pv)
    program.current_version_no = no
    session.flush()
    return pv


def _refill_attachments(
    session: Session, notice: Notice, item: dict, summary: LoadSummary, source: str
) -> None:
    version = repo.latest_version(session, notice.id)
    if version is None:
        return
    _, filled = _attach_blobs(session, version, item, DROP_PARAMS.get(source, ()))
    if not filled:
        return
    pv_id = session.scalar(
        sa.select(ItemVersion.id).where(ItemVersion.notice_version_id == version.id)
    )
    if pv_id and pv_id not in summary.refilled_version_ids:
        summary.refilled_version_ids.append(pv_id)


def _latest_raws(session: Session, source: str) -> dict[str, dict]:
    """게시물마다 가장 최근 버전의 원본 항목. 이어 둔 값도 여기에 들어 있다."""
    latest = (
        sa.select(NoticeVersion.notice_id, sa.func.max(NoticeVersion.version_no).label("no"))
        .group_by(NoticeVersion.notice_id)
        .subquery()
    )
    rows = session.execute(
        sa.select(Notice.external_id, NoticeVersion.raw)
        .join(NoticeVersion, NoticeVersion.notice_id == Notice.id)
        .join(
            latest,
            sa.and_(
                latest.c.notice_id == NoticeVersion.notice_id,
                latest.c.no == NoticeVersion.version_no,
            ),
        )
        .where(Notice.source_id == source)
    )
    return {external_id: raw for external_id, raw in rows}


def load_run(session: Session, path: Path, store: BlobStore, summary: LoadSummary) -> None:
    run = json.loads(path.read_text(encoding="utf-8"))
    at = _when(run.get("started_at"))
    run_drift: dict[str, dict] = {}
    for source, body in run["sources"].items():
        # 운영자 출처는 코드 어댑터, 사용자 출처(rss)는 등록부의 장으로 바꾼다
        to_record = TO_RECORD.get(source) or user_sources.to_record_for(session, source)
        if to_record is None:
            continue
        items = body.get("items", [])
        # 형식 변화는 어댑터가 정리한 항목으로 비교한다(DB에 남긴 항목도 정리한 것이다). 원본으로 비교하면
        # 어댑터가 이미 흡수하는 차이(예: 첨부 주소의 토큰)까지 형식 변화로 잡혀, 그 키의 진짜 변경을 가린다.
        raws = {
            it["external_id"]: to_record(json.loads(store.get(it["item_key"]))).raw for it in items
        }
        before = _latest_raws(session, source)
        watch = drift.Watch(session, source)
        if body.get("error") is None:
            held, run_drift[source] = watch.observe(path.stem, before, raws)
        else:
            held = watch.keys(drift.HELD, drift.IGNORED)  # 실패한 출처는 지켜볼 것이 없다
        seen_ids = set()
        for item in items:
            summary.items += 1
            seen_ids.add(item["external_id"])
            # 출처 형식 변화: 붙잡은 키는 마지막으로 본 값을 이어 둔다
            drift.hold(raws[item["external_id"]], before.get(item["external_id"]), held)
            raw = raws[item["external_id"]]
            # 해시는 지금 규칙으로 다시 계산한 것을 쓴다(옛 실행 파일의 해시에는 토큰이 섞여 있다).
            rec = to_record(raw)
            snap = session.scalar(
                sa.select(SnapshotRecord).where(
                    SnapshotRecord.source == source,
                    SnapshotRecord.external_id == item["external_id"],
                    SnapshotRecord.content_hash == rec.content_hash,
                )
            )
            if snap is None:
                session.add(
                    SnapshotRecord(
                        source=source,
                        external_id=item["external_id"],
                        content_hash=rec.content_hash,
                        item_key=item["item_key"],
                        first_seen_at=at,
                        last_seen_at=at,
                    )
                )
            else:
                snap.last_seen_at = at

            is_new = (
                session.scalar(
                    sa.select(Notice.id).where(
                        Notice.source_id == source, Notice.external_id == rec.external_id
                    )
                )
                is None
            )
            notice, version = repo.upsert_notice_version(session, rec)
            notice.last_seen_at = at
            notice.closed_at = None
            notice.status = "open"
            if is_new:
                notice.first_seen_at = at
                summary.new_postings += 1
                _link_item(session, notice, rec, summary)
            if version is None:
                _refill_attachments(session, notice, item, summary, source)
                continue
            summary.new_posting_versions += 1
            keys, _ = _attach_blobs(session, version, item, DROP_PARAMS.get(source, ()))
            pv = _item_version(session, notice, version, composite_hash(rec, keys), at)
            if pv is not None:
                summary.new_item_versions += 1
                summary.new_version_ids.append(pv.id)
        # 이번 실행에 없는 게시물은 닫혔다(기업마당은 정정 때 원공고를 닫는다). 피드(사용자 출처)는
        # 최근 몇 개만 보여 주므로 빠졌다고 닫지 않는다. 올린 파일의 판은 목록 전체라 닫는다(빈 판은 빼고)
        full = source in TO_RECORD or (seen_ids and user_sources.full_list(session, source))
        if body.get("error") is None and full:
            closed = session.scalars(
                sa.select(Notice).where(
                    Notice.source_id == source,
                    Notice.status == "open",
                    Notice.external_id.not_in(seen_ids) if seen_ids else sa.true(),
                )
            ).all()
            for n in closed:
                n.status, n.closed_at = "closed", at
            summary.closed += len(closed)
    session.add(LoadedRun(name=path.stem, drift=run_drift))
    if any(run_drift.values()):
        summary.drift[path.stem] = {s: d for s, d in run_drift.items() if d}
    session.flush()


def load_snapshots(session: Session, root: Path, store: BlobStore) -> LoadSummary:
    """아직 넣지 않은 실행 파일을 시간 순으로 넣는다. 실행 하나가 트랜잭션 하나다."""
    summary = LoadSummary()
    loaded = set(session.scalars(sa.select(LoadedRun.name)).all())
    for path in sorted((root / "runs").glob("*.json")):
        if path.stem in loaded:
            continue
        load_run(session, path, store, summary)
        session.commit()
        summary.runs += 1
    return summary
