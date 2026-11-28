"""사용자 출처를 등록하는 도구 (source-catalog.md 4장): 플랫폼이 먼저 제안하고, 사람이 확인한다.

    find_sources(query)     이미 있는 출처를 먼저 찾는다(운영자의 검증된 출처 + 내가 등록한 출처)
    probe_source(url)       한 번 받아 보고 초안 한 장 + 최근 글 몇 개 + 사람에게 보일 문장을 낸다(A)
    register_source(draft)  나만 쓰는 출처로 등록한다. 이용 조건에 동의하는 순간이라 늘 사람의 확인(C)
    probe_file(content)     목록 파일(CSV·JSON)을 읽어 보고 파일 올리기 출처의 초안을 낸다(A)
    drop_file(source_id, content)   내 파일 올리기 출처에 새 판을 올린다(internal, 되돌리면 그 판을 거둔다)

주소는 받을 때마다 안전한지 본다(사설·내부 주소, 비표준 포트, 넘겨주기까지). 받은 글은 신뢰할 수 없는
입력이다(probe_source는 A). 등록한 출처는 소유자의 앱만 쓴다(app_tools가 앱을 검사할 때 더한다).
RSS·Atom 피드와 게시판(html_list: 되풀이되는 글 목록의 위치를 플랫폼이 찾아 제안한다), 파일 올리기
(file_drop: 사람이 목록 파일의 새 판을 올린다. 칸 이름이 키다)를 받는다. 큰 파일은 웹에서 올린다.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from noticedelta import user_sources
from noticedelta.catalog import SourceSheet, check_sheet
from noticedelta.sources import CATALOG, file_drop, html_list, rss
from noticedelta.sources.base import SourceError
from noticedelta.tooling import Tool

SAMPLES = 5
MAX_TOOL_FILE = 500_000  # 도구로 올리는 파일(글자). 인자로 기록에 남으므로 작게, 큰 파일은 웹에서


def _now() -> datetime:
    return datetime.now(UTC)


def _person(actor: str | None) -> str | None:
    return actor.removeprefix("person:") if actor and actor.startswith("person:") else None


def suggest_id(url: str) -> str:
    u = httpx.URL(url)
    raw = f"u_{u.host}_{u.path}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug[: user_sources.MAX_ID].rstrip("_")  # 앱 정의가 쓰는 id 모양(영문 소문자·숫자·_)


def draft_sheet(url: str, title: str) -> dict:
    return {
        "kind": "source",
        "id": suggest_id(url),
        "title": (title or httpx.URL(url).host)[:100],
        "status": "private",
        "owner": "person:?",  # 등록할 때 게이트웨이가 넣은 사람으로 바뀐다
        "connector": {"kind": "rss", "url": url},
        "fetch": {"every_hours": 6, "rate_per_second": 1},
        "item": {"id": "guid", "title": "title", "url": "link"},
        "keys": {k: {"about": about} for k, about in rss.KEYS.items()},
        "terms": {"license": "등록한 사람이 이용 조건에 동의", "quote_limit": 300},
    }


def board_sheet(url: str, title: str, list_path: str) -> dict:
    sheet = draft_sheet(url, title)
    sheet["connector"] = {"kind": "html_list", "url": url, "list": list_path}
    sheet["keys"] = {k: {"about": about} for k, about in html_list.KEYS.items()}
    return sheet


def file_sheet(title: str, filename: str, fmt: str, rows: list[dict], guess: dict) -> dict:
    stem = re.sub(r"\.[a-z0-9]+$", "", filename.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", f"f_{stem}").strip("_")[: user_sources.MAX_ID].rstrip("_")
    item = {"id": guess["id"], "title": guess["title"]}
    if guess.get("url"):
        item["url"] = guess["url"]
    return {
        "kind": "source",
        "id": slug if len(slug) > 2 else "f_list",
        "title": (title or filename or "올린 목록")[:100],
        "status": "private",
        "owner": "person:?",
        "connector": {"kind": "file_drop", "format": fmt},
        "fetch": {"every_hours": 6, "rate_per_second": 1},  # 올린 파일은 수집 실행마다 읽는다
        "item": item,
        "keys": {c: {"about": f"올린 파일의 칸 「{c}」"} for c in file_drop.columns(rows)},
        "terms": {"license": "올린 사람이 쓸 권리를 가진 파일", "quote_limit": 300},
    }


def page_title(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    return " ".join(html.unescape(m[1]).split())[:100] if m else ""


def source_tools(
    factory: Callable[[], Session],
    *,
    http: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=20),
    resolve: Callable[[str], bool] = rss._public,
    now: Callable[[], datetime] = _now,
) -> list[Tool]:
    def find_sources(query: str = "", actor: str | None = None) -> dict:
        """이미 있는 출처를 찾는다(운영자의 검증된 출처 + 내가 등록한 출처). 있으면 그것을 쓴다."""
        with factory() as s:
            mine = user_sources.sheets(s, owner=_person(actor)) if _person(actor) else {}
        q = query.strip().lower()
        out = []
        for sheet in [*CATALOG.values(), *mine.values()]:
            text = f"{sheet.id} {sheet.title}".lower()
            if q and q not in text:
                continue
            out.append({
                "id": sheet.id, "title": sheet.title, "status": sheet.status,
                "keys": {k: v.about for k, v in sheet.keys.items()},
            })  # fmt: skip
        return {"sources": out}

    def unique_id(draft: dict) -> dict:
        with factory() as s:  # 다른 사람이 같은 주소를 먼저 등록했으면 겹치지 않는 id를 제안한다
            taken = user_sources.taken_ids(s, CATALOG)
        base, n = draft["id"], 2
        while draft["id"] in taken:
            tail = f"_{n}"
            draft["id"], n = base[: user_sources.MAX_ID - len(tail)] + tail, n + 1
        return draft

    def probe_source(url: str) -> dict:
        """주소를 한 번 받아 보고 출처 한 장의 초안과 최근 글, 사람에게 보일 문장을 낸다.

        RSS·Atom 피드면 피드로, 아니면 게시판(글 목록 페이지)으로 보고 되풀이되는 글 목록의 위치를 찾아
        후보로 낸다(가장 그럴듯한 것을 초안에 넣고, 나머지는 candidates로)."""
        try:
            with http() as client:
                data = rss.fetch_bytes(url, client, resolve=resolve)
        except rss.UnsafeURL as e:
            return {"error": f"받지 않는 주소: {e}"}
        except Exception as e:
            return {"error": f"받지 못했다: {e}"}
        try:
            title, items = rss.parse_feed(data)
        except SourceError:  # 피드가 아니다: 게시판으로 본다
            page = html_list.decode(data)
            try:
                found = html_list.candidates(page, url)
            except SourceError as e:
                return {"error": str(e)}
            if not found:
                return {"error": "피드도 아니고, 글 목록으로 보이는 곳도 찾지 못했다(자바스크립트로 그리는 "
                                 "페이지나 로그인이 필요한 페이지는 받지 않는다)"}  # fmt: skip
            best = found[0]
            kind = "게시판"
            draft = unique_id(board_sheet(url, page_title(page), best["list"]))
            samples = [{"title": x["title"], "published": x["date"]} for x in best["samples"]]
            count = best["count"]
            others = [{"list": c["list"], "count": c["count"],
                       "samples": [x["title"] for x in c["samples"][:2]]} for c in found[1:4]]  # fmt: skip
        else:
            kind = "피드"
            draft = unique_id(draft_sheet(url, title))
            samples = [
                {"title": it.get("title"), "published": it.get("published")}
                for it in items[:SAMPLES]
            ]
            count, others = len(items), []
        recent = ", ".join(f"「{x['title']}」" for x in samples[:2] if x["title"])
        say = f"이 {kind}에서 글 {count}개를 찾았습니다." + (
            f" 최근 글: {recent}." if recent else ""
        )
        out = {
            "draft": draft,
            "samples": samples,
            "say": say
            + " 이 출처를 지켜볼까요? 등록하면 6시간마다 받고, 이용 조건에 동의한 것으로 남습니다.",
        }
        if others:
            out["candidates"] = others  # 다른 목록 후보: 맞지 않으면 draft.connector.list를 바꾼다
        return out

    def check_draft(args: dict) -> str | None:
        data = args.get("draft")
        if not isinstance(data, dict):
            return "draft는 출처 한 장(JSON 객체)이다. probe_source가 낸 것을 고쳐 쓴다"
        sheet, found = check_sheet({**data, "owner": "person:check"})
        if sheet is None:
            return "; ".join(str(p) for p in found[:3])
        bad = user_sources.problems(sheet, ())
        return "; ".join(bad) if bad else None

    def may_register(db: Session, person_id: str | None, args: dict) -> str | None:
        if person_id is None:
            return "사람이 정해지지 않은 세션은 출처를 등록할 수 없다(내 링크로 접속한다)"
        sid = (args.get("draft") or {}).get("id")
        if sid in user_sources.taken_ids(db, CATALOG):
            # 남의 private 출처가 있다는 것은 드러내지 않는다
            return (
                f"이 id는 쓸 수 없다: {sid}. 다른 id를 쓰거나 probe_source가 새로 제안한 id를 쓴다"
            )
        return None

    def register_source(draft: dict, actor: str | None) -> dict:
        """나만 쓰는 출처로 등록한다. 사람의 확인을 받고 보류 시간 뒤에 등록된다."""
        at = now()
        data = {**draft, "owner": actor, "status": "private"}
        data["terms"] = {**draft.get("terms", {}), "agreed_by": actor,
                         "agreed_at": at.isoformat(timespec="seconds")}  # fmt: skip
        sheet = SourceSheet.model_validate(data)
        person = _person(actor)
        if person is None:
            raise PermissionError("사람이 정해지지 않았다")
        with factory() as s:
            user_sources.register(s, sheet, owner=person, at=at, operator=CATALOG, resolve=resolve)
            s.commit()
        return {
            "registered": sheet.id,
            "next": "다음 수집부터 받는다. 앱 정의의 sources에 이 id를 쓴다",
        }

    def probe_file(content: str, filename: str = "", title: str = "") -> dict:
        """목록 파일(CSV·JSON)을 읽어 보고 파일 올리기 출처의 초안과 앞의 줄 몇 개를 낸다."""
        data = content.encode("utf-8")
        fmt = file_drop.format_of(filename, data)
        try:
            rows = file_drop.parse(data, fmt)
        except SourceError as e:
            return {"error": str(e)}
        guess = file_drop.propose(rows)
        if guess["id"] is None:
            return {"error": "비지 않고 줄마다 다른 칸이 없다. 항목을 가리킬 칸(예: 번호)을 하나 만든다",
                    "columns": file_drop.columns(rows)}  # fmt: skip
        draft = unique_id(file_sheet(title, filename, fmt, rows, guess))
        samples = [{k: r.get(k, "") for k in (guess["id"], guess["title"])} for r in rows[:SAMPLES]]
        return {
            "draft": draft,
            "samples": samples,
            "say": f"이 파일에서 {len(rows)}줄을 읽었습니다. 「{guess['id']}」 칸으로 줄을 가리키고, "
            f"「{guess['title']}」 칸을 제목으로 씁니다. 이 목록을 출처로 등록할까요? 등록한 뒤 새 판을 "
            "올리면 바뀐 줄을 알려 드립니다.",
            "next": "등록(register_source)한 뒤 drop_file로 이 파일을 첫 판으로 올린다",
        }

    def may_drop(db: Session, person_id: str | None, args: dict) -> str | None:
        if person_id is None:
            return "사람이 정해지지 않은 세션은 파일을 올릴 수 없다(내 링크로 접속한다)"
        try:
            sheet = user_sources._file_source(db, str(args.get("source_id")), person_id)
            user_sources.check_drop(sheet, str(args.get("content", "")).encode("utf-8"))
        except (PermissionError, ValueError, SourceError) as e:
            return str(e)[:300]
        return None

    def small_file(args: dict) -> str | None:
        if len(str(args.get("content", ""))) > MAX_TOOL_FILE:
            return (
                f"도구로는 {MAX_TOOL_FILE:,}자까지 올린다. 큰 파일은 웹의 내 출처 화면에서 올린다"
            )
        return None

    def drop_file(source_id: str, content: str, filename: str = "upload",
                  actor: str | None = None) -> dict:  # fmt: skip
        """내 파일 올리기 출처에 새 판을 올린다. 다음 수집 실행에서 바뀐 줄을 찾는다."""
        person = _person(actor)
        if person is None:
            raise PermissionError("사람이 정해지지 않았다")
        with factory() as s:
            row, new = user_sources.drop(s, source_id, content.encode("utf-8"), filename,
                                         by=person, at=now())  # fmt: skip
            s.commit()
            out = {"source": source_id, "edition": row.seq, "rows": row.rows, "new": new}
            sheet = user_sources._file_source(s, source_id, person)
            extra = user_sources.ignored_columns(
                sheet, file_drop.parse(row.body, sheet.connector.format)
            )
            if extra:
                out["ignored_columns"] = extra  # 장에 없어 받지 않은 칸(장을 고쳐야 받는다)
            if new:
                out["next"] = "다음 수집 실행에서 이 판을 읽고 바뀐 줄을 찾는다"
                out["undo"] = {"drop_id": row.id, "by": person}
            else:
                out["next"] = "마지막 판과 같은 파일이라 새 판이 아니다"
        return out

    def undo_drop(drop_id: str, by: str) -> None:
        with factory() as s:
            user_sources.withdraw_drop(s, drop_id, by=by, at=now())
            s.commit()

    return [
        Tool(
            "find_sources", "이미 있는 출처를 찾는다(검증된 출처와 내가 등록한 출처).", find_sources
        ),
        Tool(
            "probe_file",
            "목록 파일(CSV·JSON 글)을 읽어 보고 파일 올리기 출처의 초안(칸 이름이 키, 항목 id·제목 칸 제안)"
            "과 앞의 줄 몇 개를 준다(등록하지 않는다). 공고 목록용이다: 개인정보가 든 파일은 쓰지 않는다"
            "(올린 줄은 수집한 글처럼 보관된다).",
            probe_file,
            frozenset({"A"}),
            logged_args=("filename",),
        ),
        Tool(
            "drop_file",
            "내가 등록한 파일 올리기 출처에 목록 파일의 새 판을 올린다. 되돌리면 그 판을 거둔다. 장에 적은"
            " 칸이 모두 있어야 한다. 개인정보가 든 파일은 올리지 않는다.",
            drop_file,
            effect="internal",
            undo=undo_drop,
            authorize=may_drop,
            validate=small_file,
            logged_args=("source_id", "filename"),
        ),
        Tool(
            "probe_source",
            "피드(RSS·Atom)나 게시판(글 목록 페이지) 주소를 한 번 받아 보고 출처 초안·최근 글·사람에게"
            " 보일 문장을 준다. 게시판이면 글 목록 위치의 후보도 준다(등록하지 않는다).",
            probe_source,
            frozenset({"A"}),
            logged_args=("url",),
        ),
        Tool(
            "register_source",
            "출처를 나만 쓰는 출처로 등록한다. 이용 조건에 동의하는 일이라 사람의 확인을 받는다.",
            register_source,
            frozenset({"C"}),
            effect="external",
            confirm=True,
            authorize=may_register,
            validate=check_draft,
        ),
    ]
