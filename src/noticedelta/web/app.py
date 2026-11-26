"""판정 화면 (FastAPI + Jinja2 + HTMX). 가입 없이 개인 링크(추측할 수 없는 토큰)로 쓴다.

토큰은 DB에 해시로만 있다(people.py). 화면은 토큰으로 사람 ID를 찾아 그 ID로 읽는다.

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

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from noticedelta import people, vault
from noticedelta.agent import build_gateway
from noticedelta.config import ROOT
from noticedelta.db.models import AgentSessionRow, Profile, ToolActionRow
from noticedelta.grants import SIDO
from noticedelta.metrics import collect, prometheus_text
from noticedelta.names import FIELD_NAMES, RESULT_NAMES
from noticedelta.notify import inbox
from noticedelta.tool_gateway import ToolGateway
from noticedelta.web import consent
from noticedelta.web.history_views import alert_evidence, program_detail
from noticedelta.web.ics import calendar
from noticedelta.web.views import GROUP_NAMES, GROUPS, OUTCOME_NAMES, humanize, my_programs

FLAG_NAMES = {"A": "외부 글을 읽음", "B": "내 정보를 읽음", "C": "밖으로 보낸 적 있음"}
ACTION_STATUS_NAMES = {
    "held": "승인 기다림",
    "approved": "보류 중(취소 가능)",
    "committing": "보내는 중",
    "committed": "보냄",
    "discarded": "거절·취소함",
    "expired": "기한 지남",
    "blocked": "막힘",
    "failed": "실패",
    "undone": "되돌림",
}

TEMPLATES = Path(__file__).parent / "templates"


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
    cipher: vault.Cipher | None = None,
    gateway: ToolGateway | None = None,
) -> FastAPI:
    """cipher: 개인정보 금고의 열쇠. 없으면 NOTICEDELTA_VAULT_KEY, 그것도 없으면 개발용 열쇠 파일.

    gateway: 승인함이 쓰는 도구 게이트웨이. 없으면 에이전트 도구를 모아 만든다(효과는 워커가 실행한다).
    """
    cipher = cipher or vault.load_cipher(ROOT / "data" / "vault.key")
    gate = gateway or build_gateway(session_factory, cipher=cipher)
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

    def attrs_of(session: Session, person_id: str) -> dict[str, Any]:
        """본인의 링크로 들어왔으니 자기 값을 모두 본다(금고의 self 허가, 접근 기록이 남는다)."""
        keys = vault.keys_of(session, person_id)
        if not keys:
            return {}
        g = vault.issue(
            session, person_id, consent.APP.id, vault.SELF, keys, holder="self:web", now=now()
        )
        return vault.read(session, cipher, g.id, now=now())

    def load(session: Session, token: str) -> Profile:
        person_id = people.resolve(session, token, at=now())
        if session.dirty:  # 마지막 사용 시각(하루 한 번). 다음 조회의 자동 flush보다 먼저 확정한다
            session.commit()
        profile = session.get(Profile, person_id) if person_id else None
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
        with session_factory() as s:
            person, token = people.create(s, at=now())
            # 값은 금고에 암호화해 둔다. 프로필 행에는 동의 기록만 남는다
            profile = Profile(person_id=person.id, attributes={})
            vault.put(s, cipher, person.id, attrs, source="input", now=now())
            consent.record(s, profile, research_ok=research_ok, at=now(), keys=list(attrs))
            s.add(profile)
            s.commit()
        return RedirectResponse(f"/p/{token}", status_code=303)

    @app.get("/p/{token}", response_class=HTMLResponse)
    def mine(request: Request, token: str, group: str = "eligible"):
        with session_factory() as s:
            profile = load(s, token)
            attrs = attrs_of(s, profile.person_id)
            s.commit()  # 접근 기록
            data = my_programs(s, attrs, today())
        group = group if group in GROUPS else "eligible"
        ctx = {**common, "token": token, "mine": data, "group": group, "attrs": attrs}
        return templates.TemplateResponse(request, "mine.html", ctx)

    @app.get("/p/{token}/list", response_class=HTMLResponse)
    def mine_list(request: Request, token: str, group: str = "eligible"):
        with session_factory() as s:
            profile = load(s, token)
            attrs = attrs_of(s, profile.person_id)
            s.commit()
            data = my_programs(s, attrs, today())
        group = group if group in GROUPS else "eligible"
        ctx = {**common, "token": token, "mine": data, "group": group}
        return templates.TemplateResponse(request, "_list.html", ctx)

    @app.get("/p/{token}/edit", response_class=HTMLResponse)
    def edit(request: Request, token: str):
        with session_factory() as s:
            profile = load(s, token)
            attrs, research_ok = attrs_of(s, profile.person_id), profile.research_ok
            assist_ok = consent.assist_on(s, profile.person_id)
            s.commit()
        ctx = {**common, "attrs": attrs, "token": token, "research_ok": research_ok}
        ctx["assist_ok"] = assist_ok
        return templates.TemplateResponse(request, "form.html", ctx)

    @app.post("/p/{token}/edit")
    async def save(request: Request, token: str):
        form = dict(await request.form())
        with session_factory() as s:
            profile = load(s, token)
            new = attributes_from_form(form)
            gone = {k: None for k in vault.keys_of(s, profile.person_id) - set(new)}
            vault.put(s, cipher, profile.person_id, {**gone, **new}, source="input", now=now())
            profile.attributes = {}  # 옮기기 전의 옛 값이 남아 있었다면 지운다(금고가 원본)
            # 선택 동의를 먼저 바꾼다: 끄는 동의에 새 키를 다시 적었다가 곧바로 거두지 않게
            consent.set_research(s, profile, form.get("research_ok") == "on", at=now())
            consent.set_assist(s, profile, form.get("assist_ok") == "on", at=now())
            consent.update_keys(s, profile, list(new), at=now())
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
            person_id = people.resolve(s, token)
            if person_id is None:
                raise HTTPException(404, "없는 링크입니다")
            # 에이전트 세션을 닫고 나가지 않은 효과를 버린다. 효과에 남은 인자(본문의 내 정보)도 지운다
            gate.forget_person(s, person_id, now=now())
            if not consent.delete_user(s, person_id, at=now()):
                raise HTTPException(404, "없는 링크입니다")
            people.forget(s, person_id, at=now())
            s.commit()
        return RedirectResponse("/?deleted=1", status_code=303)

    @app.get("/p/{token}/alerts", response_class=HTMLResponse)
    def alerts(request: Request, token: str):
        with session_factory() as s:
            person_id = load(s, token).person_id
            rows = [
                {
                    "id": a.id,
                    "message": a.payload.get("message", ""),
                    "sent_at": a.sent_at,
                    "status": a.status,
                }
                for a in inbox(s, person_id)
            ]
        ctx = {**common, "token": token, "alerts": rows}
        return templates.TemplateResponse(request, "alerts.html", ctx)

    @app.get("/p/{token}/alerts/{alert_id}", response_class=HTMLResponse)
    def evidence(request: Request, token: str, alert_id: str):
        with session_factory() as s:
            ev = alert_evidence(s, alert_id, load(s, token).person_id)
            if ev is None:
                raise HTTPException(404, "없는 알림입니다")
            ctx = {**common, "token": token, "ev": ev}
            return templates.TemplateResponse(request, "evidence.html", ctx)

    # --- 승인함: 에이전트가 요청한 효과를 승인·거절·고치기·취소하고, 세션을 닫는다 (5.5) ---

    def own_action(s: Session, person_id: str, action_id: str) -> ToolActionRow:
        a = s.get(ToolActionRow, action_id)
        owner = s.get(AgentSessionRow, a.session_id) if a is not None else None
        if owner is None or owner.person_id != person_id:
            raise HTTPException(404, "없는 요청입니다")
        return a

    @app.get("/p/{token}/approvals", response_class=HTMLResponse)
    def approvals(request: Request, token: str):
        with session_factory() as s:
            person_id = load(s, token).person_id
            cards = gate.inbox(s, person_id=person_id)
            sessions = s.scalars(
                sa.select(AgentSessionRow)
                .where(AgentSessionRow.person_id == person_id, AgentSessionRow.status == "open")
                .order_by(AgentSessionRow.last_seen_at.desc())
            ).all()
            done = s.scalars(
                sa.select(ToolActionRow)
                .join(AgentSessionRow, AgentSessionRow.id == ToolActionRow.session_id)
                .where(
                    AgentSessionRow.person_id == person_id,
                    ToolActionRow.status.not_in(("held", "approved")),
                )
                .order_by(ToolActionRow.requested_at.desc())
                .limit(20)
            ).all()
            ctx = {**common, "token": token, "cards": cards, "sessions": sessions, "done": done}
            ctx["FLAG_NAMES"] = FLAG_NAMES
            ctx["STATUS_NAMES"] = ACTION_STATUS_NAMES
            return templates.TemplateResponse(request, "approvals.html", ctx)

    @app.post("/p/{token}/approvals/{action_id}")
    async def decide(request: Request, token: str, action_id: str):
        form = dict(await request.form())
        do = form.get("do")
        with session_factory() as s:
            person_id = load(s, token).person_id
            own_action(s, person_id, action_id)
            by = f"person:{person_id}"
            try:
                if do == "approve":
                    gate.approve(s, action_id, by=by, now=now())
                elif do in ("reject", "cancel"):
                    why = "사람이 거절했다" if do == "reject" else "사람이 보류 중에 취소했다"
                    gate.discard(s, action_id, by=by, now=now(), why=why)
                elif do == "edit":
                    edits = {k: form[k] for k in ("body", "kind") if k in form}
                    gate.edit(s, action_id, edits, by=by, now=now())
                else:
                    raise HTTPException(400, "모르는 결정입니다")
            except (ValueError, TypeError) as e:
                s.rollback()  # 기한이 지나 닫은 것은 게이트웨이가 이미 커밋했다
                raise HTTPException(409, str(e)) from e
            s.commit()
        return RedirectResponse(f"/p/{token}/approvals", status_code=303)

    @app.post("/p/{token}/sessions/{session_id}/close")
    def close_session(token: str, session_id: str):
        with session_factory() as s:
            person_id = load(s, token).person_id
            row = s.get(AgentSessionRow, session_id)
            if row is None or row.person_id != person_id:
                raise HTTPException(404, "없는 세션입니다")
            gate.close_session(s, session_id, now=now(), by=f"person:{person_id}")
            s.commit()
        return RedirectResponse(f"/p/{token}/approvals", status_code=303)

    @app.get("/programs/{item_id}", response_class=HTMLResponse)
    def program(request: Request, item_id: str):
        with session_factory() as s:
            detail = program_detail(s, item_id)
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
            attrs = attrs_of(s, profile.person_id)
            s.commit()
            data = my_programs(s, attrs, today())
        return Response(calendar(data), media_type="text/calendar; charset=utf-8")

    return app
