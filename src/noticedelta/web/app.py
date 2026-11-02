"""판정 화면 (FastAPI + Jinja2 + HTMX). 가입 없이 개인 링크(추측할 수 없는 토큰)로 쓴다.

/                     프로필 입력과 개인정보 수집·이용 동의
/privacy              개인정보 처리 안내 (누구나)
/p/<토큰>             내 공고: 해당 · 거의 해당 · 확인 필요, 근거, 자격 변경 예정일
/p/<토큰>/list        목록 조각 (HTMX가 탭을 바꿀 때)
/p/<토큰>/edit        프로필 고치기 (선택 동의도 여기서 바꾼다)
/p/<토큰>/delete      내 정보 모두 지우기 (동의 철회)
/p/<토큰>/calendar.ics 마감일·자격 변경일 캘린더
/p/<토큰>/alerts      알림함 (정정으로 바뀐 내 판정·필드)
/p/<토큰>/alerts/<id> 알림 근거: 바뀐 원문 조각과 근거 문장
/programs/<id>        공고 상세와 변경 이력 (누구나)
/metrics              운영 지표 (Prometheus 형식, metrics_token이 있을 때만, Bearer 토큰)
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from noticedelta.db.models import Profile
from noticedelta.eligibility.rules import SIDO
from noticedelta.metrics import collect, prometheus_text
from noticedelta.names import FIELD_NAMES, RESULT_NAMES
from noticedelta.notify import inbox
from noticedelta.web import consent
from noticedelta.web.history_views import alert_evidence, program_detail
from noticedelta.web.ics import calendar
from noticedelta.web.views import GROUP_NAMES, GROUPS, OUTCOME_NAMES, humanize, my_programs

TEMPLATES = Path(__file__).parent / "templates"
TOKEN_BYTES = 18  # 개인 링크가 곧 비밀번호다. 추측할 수 없게 길게.


def _int_or_none(v: str | None) -> int | None:
    v = (v or "").replace(",", "").strip()
    return int(v) if v.lstrip("-").isdigit() else None


def _date_or_none(v: str | None) -> str | None:
    v = (v or "").strip()
    try:
        return date.fromisoformat(v).isoformat() if v else None
    except ValueError:
        return None


def attributes_from_form(form: dict[str, Any]) -> dict[str, Any]:
    """폼 값을 판정기가 읽는 속성으로. 비운 칸은 넣지 않는다(판정기가 '값 없음'으로 본다)."""
    attrs: dict[str, Any] = {
        "founded_on": _date_or_none(form.get("founded_on")),
        "ceo_birth_date": _date_or_none(form.get("ceo_birth_date")),
        "region_sido": form.get("region_sido") if form.get("region_sido") in SIDO else None,
        "business_type": form.get("business_type")
        if form.get("business_type") in ("individual", "corporation")
        else None,
        "revenue_krw": _int_or_none(form.get("revenue_krw")),
        "employees": _int_or_none(form.get("employees")),
        "is_pre_founder": form.get("is_pre_founder") == "on",
        "is_women_owned": form.get("is_women_owned") == "on",
        "is_restart": form.get("is_restart") == "on",
    }
    return {k: v for k, v in attrs.items() if v is not None}


def create_app(
    session_factory: Callable[[], Session],
    *,
    today: Callable[[], date] = date.today,
    metrics_token: str | None = None,
    contact: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    app = FastAPI(title="NoticeDelta", docs_url=None, redoc_url=None, openapi_url=None)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters["human"] = humanize
    templates = Jinja2Templates(env=env)
    common = {
        "GROUPS": GROUPS,
        "GROUP_NAMES": GROUP_NAMES,
        "OUTCOMES": OUTCOME_NAMES,
        "SIDO": SIDO,
        "RESULT_NAMES": RESULT_NAMES,
        "FIELD_NAMES": FIELD_NAMES,
        "CONSENT": consent,
        "CONTACT": contact,
    }

    def load(session: Session, token: str) -> Profile:
        profile = session.get(Profile, token)
        if profile is None:
            raise HTTPException(404, "없는 링크입니다")
        return profile

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, deleted: int = 0):
        ctx = {**common, "attrs": {}, "token": None, "deleted": bool(deleted)}
        return templates.TemplateResponse(request, "form.html", ctx)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return templates.TemplateResponse(request, "privacy.html", common)

    @app.post("/profiles")
    async def create_profile(request: Request):
        form = dict(await request.form())
        attrs = attributes_from_form(form)
        research_ok = form.get("research_ok") == "on"
        if form.get("agree") != "on":
            # 필수 동의 없이는 아무것도 저장하지 않는다. 입력한 값은 다시 채워 보여 준다.
            ctx = {**common, "attrs": attrs, "token": None, "research_ok": research_ok}
            ctx["error"] = "개인정보 수집·이용에 동의해야 내 링크를 만들 수 있습니다."
            return templates.TemplateResponse(request, "form.html", ctx, status_code=400)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with session_factory() as s:
            profile = Profile(user_id=token, attributes=attrs)
            consent.record(profile, research_ok=research_ok, at=now())
            s.add(profile)
            s.commit()
        return RedirectResponse(f"/p/{token}", status_code=303)

    @app.get("/p/{token}", response_class=HTMLResponse)
    def mine(request: Request, token: str, group: str = "eligible"):
        with session_factory() as s:
            profile = load(s, token)
            data = my_programs(s, dict(profile.attributes), today())
        group = group if group in GROUPS else "eligible"
        ctx = {**common, "token": token, "mine": data, "group": group, "attrs": profile.attributes}
        return templates.TemplateResponse(request, "mine.html", ctx)

    @app.get("/p/{token}/list", response_class=HTMLResponse)
    def mine_list(request: Request, token: str, group: str = "eligible"):
        with session_factory() as s:
            profile = load(s, token)
            data = my_programs(s, dict(profile.attributes), today())
        group = group if group in GROUPS else "eligible"
        ctx = {**common, "token": token, "mine": data, "group": group}
        return templates.TemplateResponse(request, "_list.html", ctx)

    @app.get("/p/{token}/edit", response_class=HTMLResponse)
    def edit(request: Request, token: str):
        with session_factory() as s:
            profile = load(s, token)
            attrs, research_ok = dict(profile.attributes), profile.research_ok
        ctx = {**common, "attrs": attrs, "token": token, "research_ok": research_ok}
        return templates.TemplateResponse(request, "form.html", ctx)

    @app.post("/p/{token}/edit")
    async def save(request: Request, token: str):
        form = dict(await request.form())
        with session_factory() as s:
            profile = load(s, token)
            profile.attributes = attributes_from_form(form)
            profile.research_ok = form.get("research_ok") == "on"
            s.commit()
        return RedirectResponse(f"/p/{token}", status_code=303)

    @app.get("/p/{token}/delete", response_class=HTMLResponse)
    def delete_confirm(request: Request, token: str):
        with session_factory() as s:
            load(s, token)
        return templates.TemplateResponse(request, "delete.html", {**common, "token": token})

    @app.post("/p/{token}/delete")
    def delete(token: str):
        with session_factory() as s:
            if not consent.delete_user(s, token):
                raise HTTPException(404, "없는 링크입니다")
            s.commit()
        return RedirectResponse("/?deleted=1", status_code=303)

    @app.get("/p/{token}/alerts", response_class=HTMLResponse)
    def alerts(request: Request, token: str):
        with session_factory() as s:
            load(s, token)
            rows = [
                {
                    "id": a.id,
                    "message": a.payload.get("message", ""),
                    "sent_at": a.sent_at,
                    "status": a.status,
                }
                for a in inbox(s, token)
            ]
        ctx = {**common, "token": token, "alerts": rows}
        return templates.TemplateResponse(request, "alerts.html", ctx)

    @app.get("/p/{token}/alerts/{alert_id}", response_class=HTMLResponse)
    def evidence(request: Request, token: str, alert_id: str):
        with session_factory() as s:
            load(s, token)
            ev = alert_evidence(s, alert_id, token)
            if ev is None:
                raise HTTPException(404, "없는 알림입니다")
            ctx = {**common, "token": token, "ev": ev}
            return templates.TemplateResponse(request, "evidence.html", ctx)

    @app.get("/programs/{program_id}", response_class=HTMLResponse)
    def program(request: Request, program_id: str):
        with session_factory() as s:
            detail = program_detail(s, program_id)
            if detail is None:
                raise HTTPException(404, "없는 사업입니다")
            return templates.TemplateResponse(request, "program.html", {**common, "d": detail})

    @app.get("/metrics")
    def metrics(request: Request):
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not metrics_token or not secrets.compare_digest(given, metrics_token):
            raise HTTPException(404)  # 있는지도 알리지 않는다
        with session_factory() as s:
            text = prometheus_text(collect(s))
        return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/p/{token}/calendar.ics")
    def ics(token: str):
        with session_factory() as s:
            profile = load(s, token)
            data = my_programs(s, dict(profile.attributes), today())
        return Response(calendar(data), media_type="text/calendar; charset=utf-8")

    return app
