"""판정 화면 (FastAPI + Jinja2 + HTMX). 가입 없이 개인 링크(추측할 수 없는 토큰)로 쓴다.

/                     프로필 입력
/p/<토큰>             내 공고: 해당 · 거의 해당 · 확인 필요, 근거, 자격 변경 예정일
/p/<토큰>/list        목록 조각 (HTMX가 탭을 바꿀 때)
/p/<토큰>/edit        프로필 고치기
/p/<토큰>/calendar.ics 마감일·자격 변경일 캘린더
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from noticedelta.db.models import Profile
from noticedelta.eligibility.synthetic import SIDO
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
    session_factory: Callable[[], Session], *, today: Callable[[], date] = date.today
) -> FastAPI:
    app = FastAPI(title="NoticeDelta", docs_url=None, redoc_url=None, openapi_url=None)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters["human"] = humanize
    templates = Jinja2Templates(env=env)
    common = {"GROUPS": GROUPS, "GROUP_NAMES": GROUP_NAMES, "OUTCOMES": OUTCOME_NAMES, "SIDO": SIDO}

    def load(session: Session, token: str) -> Profile:
        profile = session.get(Profile, token)
        if profile is None:
            raise HTTPException(404, "없는 링크입니다")
        return profile

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request, "form.html", {**common, "attrs": {}, "token": None}
        )

    @app.post("/profiles")
    async def create_profile(request: Request):
        form = dict(await request.form())
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with session_factory() as s:
            s.add(Profile(user_id=token, attributes=attributes_from_form(form)))
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
            attrs = dict(load(s, token).attributes)
        return templates.TemplateResponse(
            request, "form.html", {**common, "attrs": attrs, "token": token}
        )

    @app.post("/p/{token}/edit")
    async def save(request: Request, token: str):
        form = dict(await request.form())
        with session_factory() as s:
            profile = load(s, token)
            profile.attributes = attributes_from_form(form)
            s.commit()
        return RedirectResponse(f"/p/{token}", status_code=303)

    @app.get("/p/{token}/calendar.ics")
    def ics(token: str):
        with session_factory() as s:
            profile = load(s, token)
            data = my_programs(s, dict(profile.attributes), today())
        return Response(calendar(data), media_type="text/calendar; charset=utf-8")

    return app
