"""엔진의 읽기 도구 (ARCHITECTURE 5.2). 항목(사업)과 변경을 조회한다. 모두 능력 속성 A다.

    search_items   항목 찾기
    get_item       항목 하나: 원문 링크, 첨부, 뽑아 둔 필드, 버전 이력
    list_changes   최근 변경(정정) 목록
    get_change     변경 하나에서 바뀐 조각의 이전·이후

돌려주는 글은 공고·첨부의 발췌다(조각마다 MAX_TEXT자까지). 원문 전체를 내보내지 않고, 원문 링크를
함께 준다. 프로필·판정을 읽는 도구(check, explain)는 B가 붙으므로 도구 게이트웨이가 생긴 뒤에 낸다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import (
    Attachment,
    ChangeEvent,
    FieldValueRow,
    Item,
    ItemVersion,
    Notice,
    NoticeVersion,
)
from noticedelta.history import replay
from noticedelta.tooling import Tool

MAX_ITEMS = 50
MAX_PAIRS = 30
MAX_TEXT = 300
MetaReader = Callable[
    [str, dict], dict[str, Any]
]  # (출처, 원본 항목) → 구조화 값. 도메인 팩이 넘긴다

READS_DOCUMENTS = frozenset({"A"})


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value else None


def _counts(event: ChangeEvent) -> dict[str, int]:
    c = event.changed_lineages
    return {k: len(c.get(k) or []) for k in ("added", "removed", "modified", "unknown")}


def _has_change(event: ChangeEvent) -> bool:
    return any(_counts(event).values())


def _where(locator: dict | None) -> str:
    if not locator or locator.get("source") == "body":
        return "본문"
    return locator.get("filename") or "첨부"


def item_tools(
    session_factory: Callable[[], Session], *, meta: MetaReader | None = None
) -> list[Tool]:
    def postings(s: Session, item_id: str) -> list[dict]:
        rows = s.scalars(sa.select(Notice).where(Notice.item_id == item_id)).all()
        return [
            {
                "source": n.source_id,
                "external_id": n.external_id,
                "url": n.url,
                "open": n.status == "open",
            }
            for n in rows
        ]

    def summary(s: Session, p: Item) -> dict:
        posts = postings(s, p.id)
        events = s.scalars(
            sa.select(ChangeEvent).where(
                ChangeEvent.item_id == p.id, ChangeEvent.from_version_id.is_not(None)
            )
        )
        corrections = sum(_has_change(ev) for ev in events)
        return {
            "item_id": p.id,
            "title": p.title,
            "org": p.org,
            "open": any(x["open"] for x in posts),
            "versions": p.current_version_no or 0,
            "corrections": corrections,
            "url": next((x["url"] for x in posts if x["url"]), None),
        }

    def search_items(query: str = "", limit: int = 10, open_only: bool = True) -> dict:
        """제목으로 항목을 찾는다. query가 비면 최근 항목을 준다."""
        with session_factory() as s:
            q = sa.select(Item)
            if query.strip():
                q = q.where(Item.title.contains(query.strip()))
            if open_only:
                q = q.where(sa.exists().where(Notice.item_id == Item.id, Notice.status == "open"))
            total = s.scalar(sa.select(sa.func.count()).select_from(q.subquery()))
            rows = s.scalars(
                q.order_by(Item.created_at.desc()).limit(max(1, min(limit, MAX_ITEMS)))
            ).all()
            return {"total": total, "items": [summary(s, p) for p in rows]}

    def get_item(item_id: str) -> dict:
        """항목 하나의 지금 상태: 원문 링크, 첨부, 뽑아 둔 필드, 버전 이력."""
        with session_factory() as s:
            p = s.get(Item, item_id)
            if p is None:
                return {"error": f"없는 항목: {item_id}"}
            versions = s.scalars(
                sa.select(ItemVersion)
                .where(ItemVersion.item_id == p.id)
                .order_by(ItemVersion.version_no)
            ).all()
            out = {**summary(s, p), "postings": postings(s, p.id)}
            out["version_history"] = [
                {"version_no": v.version_no, "seen_at": _iso(v.source_time)} for v in versions
            ]
            if not versions:
                return out
            current = versions[-1]
            nv = s.get(NoticeVersion, current.notice_version_id)
            source = s.scalar(sa.select(Notice.source_id).where(Notice.id == nv.notice_id))
            if meta is not None:
                out["meta"] = meta(source, nv.raw)
            atts = s.scalars(sa.select(Attachment).where(Attachment.notice_version_id == nv.id))
            out["attachments"] = [
                {"filename": a.filename, "role": a.role, "parse_status": a.parse_status}
                for a in atts
            ]
            fields = s.scalars(
                sa.select(FieldValueRow).where(FieldValueRow.item_version_id == current.id)
            )
            out["fields"] = [
                {"name": f.name, "value": f.value, "status": f.status, "confidence": f.confidence}
                for f in fields
            ]
            return out

    def list_changes(since: str = "", item_id: str = "", limit: int = 20) -> dict:
        """최근 변경(정정)을 새것부터. since는 날짜(YYYY-MM-DD), item_id로 한 항목만 볼 수 있다."""
        try:
            after = datetime.fromisoformat(since) if since.strip() else None
        except ValueError:
            return {"error": f"날짜 형식이 아니다(YYYY-MM-DD): {since}"}
        with session_factory() as s:
            q = (
                sa.select(ChangeEvent, ItemVersion, Item.title)
                .join(ItemVersion, ItemVersion.id == ChangeEvent.to_version_id)
                .join(Item, Item.id == ChangeEvent.item_id)
                .where(ChangeEvent.from_version_id.is_not(None))
                .order_by(ItemVersion.source_time.desc(), ChangeEvent.id)
            )
            if item_id.strip():
                q = q.where(ChangeEvent.item_id == item_id.strip())
            if after is not None:
                q = q.where(ItemVersion.source_time >= after)
            changes = []
            for ev, pv, title in s.execute(q):
                if not _has_change(ev):
                    continue  # 원본의 다른 값만 바뀌고 읽는 글은 그대로인 버전
                changes.append(
                    {
                        "event_id": ev.id,
                        "item_id": ev.item_id,
                        "title": title,
                        "seen_at": _iso(pv.source_time),
                        "to_version": pv.version_no,
                        "segments": _counts(ev),
                    }
                )
                if len(changes) >= max(1, min(limit, MAX_ITEMS)):
                    break
            return {"changes": changes}

    def get_change(event_id: str, max_pairs: int = 10) -> dict:
        """변경 하나에서 바뀐 조각의 이전·이후 글(발췌)과 위치."""
        with session_factory() as s:
            ev = s.get(ChangeEvent, event_id)
            if ev is None:
                return {"error": f"없는 변경: {event_id}"}
            step = next((x for x in replay(s, ev.item_id) if x.event.id == ev.id), None)
            if step is None:
                return {"error": f"다시 만들 수 없는 변경: {event_id}"}
            title = s.scalar(sa.select(Item.title).where(Item.id == ev.item_id))
            pairs = []
            for old, new in step.pairs():
                kind = "modified" if old and new else ("added" if new else "removed")
                where = _where((new or old).locator)
                pairs.append(
                    {
                        "change": kind,
                        "where": where,
                        "before": _clip(old.text if old else None),
                        "after": _clip(new.text if new else None),
                    }
                )
            shown = pairs[: max(1, min(max_pairs, MAX_PAIRS))]
            return {
                "event_id": ev.id,
                "item_id": ev.item_id,
                "title": title,
                "to_version": step.to_version.version_no,
                "seen_at": _iso(step.to_version.source_time),
                "segments": _counts(ev),
                "pairs": shown,
                "more": len(pairs) - len(shown),
            }

    described = [
        (search_items, "수집해 둔 항목(공고 등)을 제목으로 찾는다. 열린 항목만 볼지 고를 수 있다."),
        (get_item, "항목 하나의 원문 링크, 첨부, 뽑아 둔 필드, 버전 이력을 준다."),
        (list_changes, "최근에 바뀐 항목(정정)의 목록을 준다."),
        (get_change, "변경 하나에서 무엇이 어떻게 바뀌었는지 조각의 이전·이후 글로 준다."),
    ]
    # 사용 기록(usage.py)이 결과 개수를 세는 칸과, 남겨도 되는 인자(검색어는 새 출처의 수요 신호다)
    extra = {
        "search_items": {"count": "total", "logged_args": ("query",)},
        "list_changes": {"count": "changes"},
        "get_change": {"count": "pairs"},
    }
    return [
        Tool(fn.__name__, text, fn, READS_DOCUMENTS, **extra.get(fn.__name__, {}))
        for fn, text in described
    ]
