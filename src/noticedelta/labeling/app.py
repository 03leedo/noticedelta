"""정답 라벨 화면 (작업 목록 5a). 내 PC에서만 띄우는 도구다(127.0.0.1).

/                 라벨할 정정 목록과 회차별 진행
/e/<버전>?round=1 원문 비교(API 값·본문·첨부 원본)와 필드별 라벨 입력
/round2           7일 지난 1차 라벨 중 30% 다시 라벨
/agreement        1차·2차 일치도 (Cohen's kappa)
/blob/<키>        첨부 원본 내려받기
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from noticedelta.blobstore import BlobNotFound, BlobStore
from noticedelta.db.models import LabelRow
from noticedelta.labeling.store import (
    FIELDS,
    STATUSES,
    VISIBLE,
    agreement,
    event_view,
    events,
    save_labels,
    second_round,
)
from noticedelta.names import FIELD_NAMES

HERE = Path(__file__).parent
WEB_TEMPLATES = HERE.parent / "web" / "templates"  # base.html을 같이 쓴다
_HEX = re.compile(r"[0-9a-f]{64}")


def _now() -> datetime:
    return datetime.now(UTC)


def create_label_app(
    session_factory: Callable[[], Session],
    store: BlobStore,
    *,
    labeler: str = "me",
    now: Callable[[], datetime] = _now,
) -> FastAPI:
    app = FastAPI(title="NoticeDelta 라벨", docs_url=None, redoc_url=None, openapi_url=None)
    env = Environment(
        loader=FileSystemLoader([HERE / "templates", WEB_TEMPLATES]),
        autoescape=select_autoescape(["html"]),
    )
    templates = Jinja2Templates(env=env)
    common = {
        "FIELDS": FIELDS,
        "FIELD_NAMES": FIELD_NAMES,
        "STATUSES": STATUSES,
        "VISIBLE": VISIBLE,
    }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with session_factory() as s:
            evs = events(s, labeler)
            due = {e.to_version.id for e in second_round(s, now(), labeler=labeler)}
        ctx = {**common, "events": evs, "due": due}
        return templates.TemplateResponse(request, "label_index.html", ctx)

    @app.get("/e/{version_id}", response_class=HTMLResponse)
    def event(request: Request, version_id: str, round: int = 1):
        if round not in (1, 2):
            raise HTTPException(400, "회차는 1 또는 2")
        with session_factory() as s:
            view = event_view(s, version_id, labeler)
            if view is None:
                raise HTTPException(404, "없는 정정입니다")
            saved = {
                r.field: r
                for r in s.scalars(
                    sa.select(LabelRow).where(
                        LabelRow.to_version_id == version_id,
                        LabelRow.round == round,
                        LabelRow.labeler == labeler,
                    )
                )
            }
        # 2차에서는 1차 답을 보여 주지 않는다(같은 회차에 저장한 값만 다시 채운다).
        ctx = {**common, "v": view, "round": round, "saved": saved}
        return templates.TemplateResponse(request, "label_event.html", ctx)

    @app.post("/e/{version_id}")
    async def save(request: Request, version_id: str, round: int = 1):
        form = await request.form()
        rows = {
            name: {
                k: form.get(f"{name}.{k}")
                for k in ("status", "before", "after", "visible_in", "evidence")
            }
            for name in FIELDS
        }
        rows[FIELDS[0]]["note"] = form.get("note")
        with session_factory() as s:
            view = event_view(s, version_id, labeler)
            if view is None:
                raise HTTPException(404, "없는 정정입니다")
            save_labels(s, view.ref, round=round, labeler=labeler, rows=rows, now=now())
            s.commit()
        return RedirectResponse("/" if round == 1 else "/round2", status_code=303)

    @app.get("/round2", response_class=HTMLResponse)
    def round2(request: Request):
        with session_factory() as s:
            due = second_round(s, now(), labeler=labeler)
        return templates.TemplateResponse(request, "label_round2.html", {**common, "due": due})

    @app.get("/agreement", response_class=HTMLResponse)
    def agree(request: Request):
        with session_factory() as s:
            a = agreement(s, labeler)
        return templates.TemplateResponse(request, "label_agreement.html", {**common, "a": a})

    @app.get("/blob/{key}")
    def blob(key: str, name: str = "attachment"):
        if not _HEX.fullmatch(key):
            raise HTTPException(400, "잘못된 키")
        try:
            data = store.get(key)
        except BlobNotFound as e:
            raise HTTPException(404, "원본이 없습니다") from e
        safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", name)[:120] or "attachment"
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe, safe='')}"},
        )

    return app
