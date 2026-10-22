"""상시 수집 스냅샷을 DB에 넣고 게시물을 사업(program)으로 묶는다 (설계 6.1, 작업 목록 10번).

실행 파일(runs/<시각>.json)을 시간 순으로 한 번씩만 읽는다.

1. 스냅샷 기록: (출처, 외부 ID, 내용 해시)마다 처음·마지막으로 본 시각.
2. 게시물 버전: 내용이 바뀐 공고만 notice_version을 만든다(repo.upsert_notice_version).
   첨부는 다시 받지 않고, 수집기가 이미 받은 원본 키를 그대로 쓴다.
3. 사업 연결: 새 게시물은 같은 기관의 기존 게시물 중 정정 표시를 뺀 제목이 같은 것과 잇는다
   (기업마당 재게시). 제목이 비슷하기만 하면 합치지 않고 needs_review로 남긴다.
   잘못 합치는 것이 놓치는 것보다 위험하다(남의 자격 조건이 내 사업으로 들어온다).
4. 사업 버전: 본문 해시 + 첨부 내용 해시가 바뀔 때만 새 버전. 번호는 실행 시간 순이다.
5. 이번 실행에 없는 게시물은 닫힌 것으로 표시한다(그 출처가 오류 없이 끝났을 때만).
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

from noticedelta.blobstore import BlobStore
from noticedelta.db import repo
from noticedelta.db.models import (
    Attachment,
    LoadedRun,
    Notice,
    NoticeVersion,
    Program,
    ProgramLink,
    ProgramVersion,
    SnapshotRecord,
)
from noticedelta.sources import NoticeRecord, bizinfo, kstartup

TO_RECORD = {"kstartup": kstartup.to_record, "bizinfo": bizinfo.to_record}
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


def composite_hash(rec: NoticeRecord, blob_keys: list[str | None]) -> str:
    """사업 버전의 내용 해시: 게시물 본문 해시 + 첨부 내용 해시(받지 못한 첨부는 URL로)."""
    parts = [rec.content_hash] + sorted(k or "missing" for k in blob_keys)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _when(value: str | None) -> datetime:
    return datetime.fromisoformat(value) if value else datetime.now()


@dataclass
class LoadSummary:
    runs: int = 0
    items: int = 0
    new_postings: int = 0
    new_posting_versions: int = 0
    new_programs: int = 0
    new_program_versions: int = 0
    links: Counter = field(default_factory=Counter)
    closed: int = 0


def _link_program(
    session: Session, notice: Notice, rec: NoticeRecord, summary: LoadSummary
) -> None:
    key = title_key(rec.title)
    org = rec.org
    best, best_score = None, 0.0
    if org:
        candidates = session.scalars(
            sa.select(Notice).where(
                Notice.source_id == notice.source_id,
                Notice.org == org,
                Notice.id != notice.id,
                Notice.program_id.is_not(None),
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
        notice.program_id = best.program_id
        decision, method = "linked", "repost_title"
    else:
        program = Program(title=rec.title, title_key=key, org=org)
        session.add(program)
        session.flush()
        notice.program_id = program.id
        summary.new_programs += 1
        decision, method = "linked", "new"
        if best is not None and best_score >= REVIEW_LINK:
            # 비슷하지만 같다고 확신할 수 없다. 따로 두고 확인 목록에 올린다.
            session.add(
                ProgramLink(
                    notice_id=notice.id,
                    program_id=best.program_id,
                    method="repost_title",
                    score=round(best_score, 3),
                    decision="needs_review",
                    candidate_notice_id=best.id,
                )
            )
            summary.links["needs_review"] += 1
    session.add(
        ProgramLink(
            notice_id=notice.id,
            program_id=notice.program_id,
            method=method,
            score=round(best_score, 3) if method != "new" else 1.0,
            decision=decision,
            candidate_notice_id=best.id if method != "new" and best else None,
        )
    )
    summary.links[method] += 1


def _attach_blobs(session: Session, version: NoticeVersion, item: dict) -> list[str | None]:
    """수집기가 받은 첨부 원본 키를 첨부 행에 옮긴다 (lineage 키는 repo가 버전을 만들 때 붙인다)."""
    by_url = {a["url"]: a for a in item.get("attachments", [])}
    rows = session.scalars(
        sa.select(Attachment).where(Attachment.notice_version_id == version.id)
    ).all()
    keys = []
    for att in rows:
        snap = by_url.get(att.url, {})
        if snap.get("blob_key") and att.blob_key is None:
            att.blob_key, att.mime = snap["blob_key"], snap.get("mime")
        keys.append(att.blob_key)
    return keys


def _program_version(
    session: Session, notice: Notice, version: NoticeVersion, chash: str, at: datetime
) -> bool:
    program = session.get(Program, notice.program_id)
    exists = session.scalar(
        sa.select(ProgramVersion.id).where(
            ProgramVersion.program_id == program.id, ProgramVersion.content_hash == chash
        )
    )
    if exists:
        return False  # 늦게 온 옛 내용이거나 같은 내용의 재게시
    no = (program.current_version_no or 0) + 1
    session.add(
        ProgramVersion(
            program_id=program.id,
            version_no=no,
            notice_version_id=version.id,
            content_hash=chash,
            source_time=at,
        )
    )
    program.current_version_no = no
    return True


def load_run(session: Session, path: Path, store: BlobStore, summary: LoadSummary) -> None:
    run = json.loads(path.read_text(encoding="utf-8"))
    at = _when(run.get("started_at"))
    for source, body in run["sources"].items():
        to_record = TO_RECORD.get(source)
        if to_record is None:
            continue
        seen_ids = set()
        for item in body.get("items", []):
            summary.items += 1
            seen_ids.add(item["external_id"])
            snap = session.scalar(
                sa.select(SnapshotRecord).where(
                    SnapshotRecord.source == source,
                    SnapshotRecord.external_id == item["external_id"],
                    SnapshotRecord.content_hash == item["content_hash"],
                )
            )
            if snap is None:
                session.add(
                    SnapshotRecord(
                        source=source,
                        external_id=item["external_id"],
                        content_hash=item["content_hash"],
                        item_key=item["item_key"],
                        first_seen_at=at,
                        last_seen_at=at,
                    )
                )
            else:
                snap.last_seen_at = at

            rec = to_record(json.loads(store.get(item["item_key"])))
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
                _link_program(session, notice, rec, summary)
            if version is None:
                continue
            summary.new_posting_versions += 1
            keys = _attach_blobs(session, version, item)
            if _program_version(session, notice, version, composite_hash(rec, keys), at):
                summary.new_program_versions += 1
        if body.get("error") is None:
            # 이번 실행에 없는 게시물은 닫혔다(기업마당은 정정 때 원공고를 닫는다).
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
    session.add(LoadedRun(name=path.stem))
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


# --- 버전 완전성 (DESIGN 7.3) ---

SETTLED = {"ok", "needs_review", "unsupported"}


def attachments_settled(session: Session, notice_version_id: str) -> tuple[bool, list[str]]:
    """새 버전의 첨부가 모두 끝 상태인지. 아직 받지 못했거나 파싱 전인 첨부 이름을 함께 준다.

    끝나지 않은 첨부가 있는 버전은 조각 비교를 하지 않는다. 일시 장애로 빠진 첨부의 조각이
    "삭제"로 잡혀 자격 조건이 사라진 것처럼 보이는 가짜 알림을 막는다.
    """
    rows = session.scalars(
        sa.select(Attachment).where(Attachment.notice_version_id == notice_version_id)
    ).all()
    waiting = [a.filename for a in rows if a.blob_key is None or a.parse_status not in SETTLED]
    return not waiting, waiting


def unreadable_attachments(session: Session, notice_version_id: str) -> set[str]:
    """파싱해 조각을 만들지 못한 첨부의 lineage 키 (needs_review, unsupported)."""
    rows = session.scalars(
        sa.select(Attachment).where(
            Attachment.notice_version_id == notice_version_id, Attachment.parse_status != "ok"
        )
    ).all()
    return {a.lineage_key for a in rows if a.lineage_key}


def comparable_old_segments(old_segments, unreadable: set[str]):
    """새 버전에서 읽지 못한 첨부의 옛 조각은 비교에서 빼고 "알 수 없음" lineage로 돌려준다.

    그 첨부가 정말 바뀌었는지 모르므로 삭제로 보지 않는다. 엔진은 이 lineage에 걸린 필드를
    stale로 낮추고 알리지 않는다(impact.run_impact의 unknown_lineages).
    """
    kept, unknown = [], set()
    for seg in old_segments:
        if seg.locator.get("attachment") in unreadable:
            unknown.add(seg.lineage_id)
        else:
            kept.append(seg)
    return kept, unknown
