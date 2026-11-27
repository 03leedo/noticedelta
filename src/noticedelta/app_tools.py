"""앱을 만드는 조종 도구 (app-spec 3장, platform-services 5.7): 에이전트가 앱을 만들고 사람이 확인한다.

- app_schema: 앱 정의의 JSON 스키마(공짜).
- check_app(definition): 검증만 한다. 고칠 방법이 적힌 문제 목록(공짜, 아무것도 남기지 않음).
- save_app(definition): 초안 판을 등록부에 넣는다. 켜지 않는다. 플랫폼 안의 되돌릴 수 있는 효과(internal)라
  바로 반영하고 되돌릴 수 있다. 새 앱이면 부른 사람이 소유자가 된다. 있는 앱은 소유자만 판을 더한다.
- preview_app(app_id, version): 기록된 정정을 초안으로 다시 흘려 켠 판과 알림을 비교한다(녹화본만, 비용 0).
- app_status(app_id): 앱의 상태 보고(app-spec 11장). 운영자의 앱은 누구나, 다른 앱은 소유자만.
- set_app_budget(app_id, daily_tokens): 앱의 하루 LLM 한도. 예산 올리기는 사람의 확인이 필요한 순간이라
  늘 소유자의 확인을 받는다(C, confirm).
- enable_app(app_id, version): 판을 켠다. 구독자 모두의 알림이 바뀌는 일이라 밖으로 나가는 효과(C)로
  두고 늘 소유자의 확인을 받는다(confirm). 승인함에 붙잡혔다가 보류 시간 뒤에 켜진다.

부른 사람(actor)은 게이트웨이가 넣는다. 사람이 정해지지 않은 세션은 앱을 저장하거나 켤 수 없다.
운영자가 저장소에서 넣은 앱(지원사업 앱)은 소유자 행이 없어 에이전트가 고칠 수 없다.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from noticedelta import app_registry, user_sources
from noticedelta.appspec import AppDefinition, check_app
from noticedelta.db.models import AppDefinitionRow, AppMemberRow
from noticedelta.grants import APP
from noticedelta.sources import CATALOG
from noticedelta.tooling import Tool

Preview = Callable[[AppDefinition, AppDefinition | None], dict]
# 운영자가 저장소에서 넣는 앱의 id. DB에 아직 없어도 아무도 이 id로 앱을 만들 수 없다
RESERVED = frozenset({APP.id})


def _now() -> datetime:
    return datetime.now(UTC)


def _problems(
    definition: dict, extra: dict | None = None
) -> tuple[AppDefinition | None, list[dict]]:
    """extra: 그 사람이 등록한 출처(사용자 출처는 소유자의 앱만 쓴다)."""
    sheets = {**CATALOG, **(extra or {})}
    app, problems = check_app(
        definition,
        sources={sid: s.readable_keys() for sid, s in sheets.items()},
        meta={sid: set(s.meta) for sid, s in sheets.items()},
    )
    return app, [{"where": p.path, "what": p.message, "fix": p.fix} for p in problems]


def _person(actor: str | None) -> str | None:
    return actor.removeprefix("person:") if actor and actor.startswith("person:") else None


def owner_only(db: Session, person_id: str | None, args: dict) -> str | None:
    """있는 앱은 소유자만, 새 앱은 사람이 정해진 세션이면 누구나(그 사람이 소유자가 된다)."""
    app_id = args.get("app_id") or (args.get("definition") or {}).get("id")
    if person_id is None:
        return "사람이 정해지지 않은 세션은 앱을 만들거나 고칠 수 없다(내 링크로 접속한다)"
    if not isinstance(app_id, str):
        return "앱 id가 없다"
    if app_id in RESERVED:
        return f"{app_id}는 운영자의 앱이라 소유자만 할 수 있다(에이전트는 고칠 수 없다)"
    if app_registry.exists(db, app_id) and app_registry.OWNER not in app_registry.roles_of(
        db, app_id, person_id
    ):
        return f"{app_id}의 소유자만 할 수 있다"
    return None


def backtest_preview(
    cassettes: Path, factory: Callable[[], Session], *, model: str = "pickle-general"
) -> Preview:
    """기록된 정정(개발 세트)을 녹화본으로 다시 흘린다. 녹화본이 없는 정정은 '녹화본 없음'으로 센다.

    게이트웨이와 같은 DB(factory)에서 읽는다(따로 열거나 마이그레이션하지 않는다).
    """

    def preview(draft: AppDefinition, on: AppDefinition | None) -> dict:
        from noticedelta.backtest import backtest, compare
        from noticedelta.eligibility.synthetic import virtual_profiles
        from noticedelta.evaluation import load_chains, load_labels
        from noticedelta.llm import FileCassetteStore, Gateway, Mode
        from noticedelta.llm.extract import TEMPLATE_V1, LLMExtractor, spec_for
        from noticedelta.programs import api_meta

        gateway = Gateway(None, FileCassetteStore(cassettes), Mode.REPLAY)
        with factory() as s:
            chains = load_chains(s, split="dev", meta=api_meta)
            labels = load_labels(s, labeler="claude")
        profiles = {f"v{i:03d}": p for i, p in enumerate(virtual_profiles(20))}

        def run(app: AppDefinition):
            ext = LLMExtractor(gateway, model=model, template=TEMPLATE_V1, spec=spec_for(app))
            return backtest(app, chains, ext, profiles, usage=lambda: gateway.usage)

        d = run(draft)
        out: dict[str, Any] = {"draft": d.app, "events": d.events, "summary": d.summary()}
        out["missing_cassettes"] = len(d.missing)
        if on is not None:
            base = run(on)
            c = compare(base, d, labels)
            out["against"] = f"{on.id}@{on.version}"
            out["against_summary"] = base.summary()  # 나란히 보기(검증 화면)
            out["against_missing"] = len(base.missing)
            out["diff"] = {
                "new": len(c.new),
                "gone": len(c.gone),
                "changed": len(c.changed),
                "broken_tests": len(c.broken),
                "fake_new": len(c.fake_new),
                "right_new": len(c.right_new),
            }
        return out

    return preview


def existing_owner(db: Session, person_id: str | None, args: dict) -> str | None:
    """있는 앱의 소유자만(켜기·미리 돌려 보기·예산). 없는 앱이면 막는다."""
    why = owner_only(db, person_id, args)
    if why is None and not app_registry.exists(db, str(args.get("app_id"))):
        return f"없는 앱: {args.get('app_id')}. save_app으로 먼저 넣는다"
    return why


def readable(db: Session, person_id: str | None, args: dict) -> str | None:
    """상태 보고: 운영자의 앱은 누구나(사람 수만 센 숫자다), 다른 앱은 소유자만."""
    if args.get("app_id") in RESERVED:
        return None
    return existing_owner(db, person_id, args)


def app_tools(
    factory: Callable[[], Session],
    *,
    preview: Preview | None = None,
    now: Callable[[], datetime] = _now,
    snapshots: Path | None = None,
    daily: int = 200_000,
    known: Callable[[], Collection[str]] | None = None,
) -> list[Tool]:
    """known: 플랫폼이 내놓는 도구 이름(앱 정의의 agent.tools를 검사한다)."""

    def app_schema() -> dict:
        """앱 정의의 JSON 스키마. 이것을 보고 정의를 쓴 뒤 check_app으로 검사한다."""
        return {"schema": AppDefinition.model_json_schema()}

    def _unknown_tools(app: AppDefinition) -> dict | None:
        """검증을 지난 정의의 agent.tools 가운데 이 플랫폼에 없는 도구(모양은 규격이 이미 봤다)."""
        if known is None or app.agent is None:
            return None
        unknown = sorted(set(app.agent.tools) - set(known()))
        if not unknown:
            return None
        return {"where": "agent.tools", "what": f"없는 도구: {unknown}",
                "fix": f"쓸 수 있는 도구: {sorted(known())}"}  # fmt: skip

    def mine(actor: str | None) -> dict:
        """그 사람이 등록한 출처(앱 정의가 쓸 수 있다)."""
        person = _person(actor)
        if person is None:
            return {}
        with factory() as s:
            return user_sources.sheets(s, owner=person)

    def check(definition: dict, actor: str | None = None) -> dict:
        """앱 정의를 검사만 한다. 문제마다 어디가, 무엇이, 어떻게 고치는지 준다."""
        data = copy.deepcopy(definition)
        if isinstance(data, dict):  # 소유자 칸은 저장할 때 게이트웨이가 채운다
            data.setdefault("owner", "person:check")
        app, problems = _problems(data, mine(actor))
        if app is not None and (bad := _unknown_tools(app)):
            problems.append(bad)
            app = None
        return {"ok": app is not None, "problems": problems}

    def validate_definition(args: dict) -> str | None:
        if not isinstance(args.get("definition"), dict):
            return "definition은 앱 정의(JSON 객체)다"
        return None

    def may_save(db: Session, person_id: str | None, args: dict) -> str | None:
        """저장 전에 소유자와 정의를 본다(문제가 있으면 넣지 않는다. 자세한 목록은 check_app이 준다).

        그 사람이 등록한 출처를 게이트웨이의 DB 세션으로 읽는다(새 세션을 열면 SQLite에서 잠긴다).
        """
        why = owner_only(db, person_id, args)
        if why is not None:
            return why
        data = {**copy.deepcopy(args["definition"]), "owner": "person:check"}
        own = user_sources.sheets(db, owner=person_id) if person_id else {}
        app, problems = _problems(data, own)
        if app is not None and (bad := _unknown_tools(app)):
            problems.append(bad)
        if problems:
            shown = "; ".join(f"{p['where']}: {p['what']}" for p in problems[:3])
            return f"정의에 문제 {len(problems)}개(check_app으로 자세히): {shown}"
        return None

    def save_app(definition: dict, actor: str | None) -> dict:
        """초안 판을 넣는다(켜지 않는다). 새 앱이면 부른 사람이 소유자가 된다."""
        data = copy.deepcopy(definition)
        data["owner"] = actor  # 정의의 소유자 칸은 게이트웨이가 넣은 사람으로
        app, problems = _problems(data, mine(actor))
        if app is None:  # validate를 지났으면 오지 않는다. 실패는 예외로(효과를 남기지 않게)
            raise ValueError("; ".join(f"{p['where']}: {p['what']}" for p in problems[:3]))
        person = _person(actor)
        with factory() as s:
            new = not app_registry.exists(s, app.id)
            # 같은 트랜잭션에서 다시 본다: 권한을 확인한 뒤 남이 먼저 같은 id를 만들었을 수 있다
            if not new and app_registry.OWNER not in app_registry.roles_of(s, app.id, person):
                raise PermissionError(f"{app.id}의 소유자만 판을 더한다")
            created = s.get(AppDefinitionRow, (app.id, app.version)) is None
            app_registry.register(s, app, at=now())  # 같은 판·다른 내용이면 VersionConflict
            if new and person is not None:
                app_registry.add_member(s, app.id, person, app_registry.OWNER, at=now())
            s.commit()
        out = {
            "ok": True,
            "app_id": app.id,
            "version": app.version,
            "content_hash": app.content_hash(),
            "next": "preview_app으로 켠 판과 비교한 뒤 enable_app으로 켠다(소유자의 확인을 받는다)",
        }
        if created:  # 이번에 넣은 판만 되돌린다(같은 내용을 다시 저장한 것은 되돌릴 것이 없다)
            out["undo"] = {
                "app_id": app.id,
                "version": app.version,
                "owner": person if new else None,
            }
        return out

    def undo_save(app_id: str, version: int, owner: str | None = None) -> None:
        with factory() as s:
            if not app_registry.drop_draft(s, app_id, version):
                raise ValueError(f"{app_id} 판 {version}은 켰거나 없어 되돌릴 수 없다")
            if owner is not None and not app_registry.exists(s, app_id):
                # 이 저장이 앱을 만들었고 남은 판이 없다: 그때 생긴 소유자 행도 지운다
                row = s.get(AppMemberRow, (app_id, owner, app_registry.OWNER))
                if row is not None:
                    s.delete(row)
            s.commit()

    def preview_app(app_id: str, version: int, actor: str | None) -> dict:
        """초안 판으로 기록된 정정을 다시 흘려 켠 판과 알림을 비교한다(녹화본만, 비용 0)."""
        with factory() as s:
            row = s.get(AppDefinitionRow, (app_id, version))
            if row is None:
                return {"error": f"{app_id} 판 {version}이 없다. save_app으로 먼저 넣는다"}
            draft = AppDefinition.model_validate(row.body)
            on = app_registry.current(s, app_id)
        if preview is None:
            return {"error": "이 서버에서는 미리 돌려 보기를 쓸 수 없다"}
        return preview(draft, on if on is not None and on.version != version else None)

    def validate_version(args: dict) -> str | None:
        if not isinstance(args.get("app_id"), str) or not isinstance(args.get("version"), int):
            return "app_id는 글자, version은 정수다"
        return None

    def enable_app(app_id: str, version: int, actor: str | None) -> dict:
        """판을 켠다. 소유자의 확인을 받고 보류 시간이 지나야 켜진다."""
        with factory() as s:  # 확정 때 다시 본다(그 사이 소유자가 바뀌었을 수 있다)
            if app_registry.OWNER not in app_registry.roles_of(s, app_id, _person(actor)):
                raise PermissionError(f"{app_id}의 소유자가 아니다")
            app_registry.enable(s, app_id, version, at=now())
            s.commit()
        return {"enabled": f"{app_id}@{version}"}

    def validate_status(args: dict) -> str | None:
        days = args.get("days", 7)
        if not isinstance(args.get("app_id"), str) or type(days) is not int or not 1 <= days <= 90:
            return "app_id는 글자, days는 1~90의 정수다"
        return None

    def app_status(app_id: str, days: int = 7) -> dict:
        """앱의 상태: 수집·형식 변화·파싱·붙잡힌 알림·예산을 숫자와 사람 말로."""
        from datetime import timedelta, timezone

        from noticedelta.app_status import app_status as status
        from noticedelta.app_status import describe

        with factory() as s:
            app = app_registry.current(s, app_id) or (APP if app_id == APP.id else None)
            if app is None:
                return {"error": f"켠 판이 없는 앱: {app_id}"}
            at = now()
            kst = timezone(timedelta(hours=9))
            today = at.astimezone(kst).replace(hour=0, minute=0, second=0, microsecond=0)
            today = today.astimezone(UTC)  # 기록은 UTC로 적혀 있다(SQLite는 글자로 비교한다)
            out = status(s, app, root=snapshots, since=at - timedelta(days=days),
                         now=at, primary=APP.id, daily=daily, today=today)  # fmt: skip
        return {**out, "summary": describe(out)}

    def validate_budget(args: dict) -> str | None:
        from noticedelta.budgets import others_cap

        tokens, cap = args.get("daily_tokens"), others_cap(daily)
        if not isinstance(args.get("app_id"), str) or type(tokens) is not int:
            return "app_id는 글자, daily_tokens는 정수다"
        if not 0 <= tokens <= cap:
            return f"daily_tokens는 0~{cap:,}다(다른 앱들이 함께 쓰는 몫. 넓히려면 운영자에게)"
        return None

    def set_app_budget(app_id: str, daily_tokens: int, actor: str | None) -> dict:
        """앱의 하루 LLM 토큰 한도를 정한다. 소유자의 확인을 받고 보류 시간 뒤에 바뀐다."""
        with factory() as s:
            if app_registry.OWNER not in app_registry.roles_of(s, app_id, _person(actor)):
                raise PermissionError(f"{app_id}의 소유자가 아니다")
            before = app_registry.set_daily_tokens(
                s, app_id, daily_tokens, by=actor or "", at=now()
            )
            s.commit()
        return {"app_id": app_id, "daily_tokens": daily_tokens, "before": before}

    return [
        Tool("app_schema", "앱 정의의 JSON 스키마를 준다.", app_schema),
        Tool(
            "app_status",
            "앱의 상태(수집·형식 변화·파싱·붙잡힌 알림·예산)를 숫자와 사람 말 요약으로 준다.",
            app_status,
            authorize=readable,
            validate=validate_status,
        ),
        Tool(
            "set_app_budget",
            "앱의 하루 LLM 토큰 한도를 정한다. 소유자의 확인을 받고 보류 시간 뒤에 바뀐다.",
            set_app_budget,
            frozenset({"C"}),
            effect="external",
            confirm=True,
            authorize=existing_owner,
            validate=validate_budget,
        ),
        Tool(
            "check_app",
            "앱 정의를 검사만 한다. 문제마다 어디가·무엇이·어떻게 고치는지 준다. 아무것도 남기지 않는다.",
            check,
        ),
        Tool(
            "save_app",
            "앱 정의의 초안 판을 넣는다(켜지 않는다). 새 앱이면 내가 소유자가 된다. 되돌릴 수 있다.",
            save_app,
            effect="internal",
            undo=undo_save,
            authorize=may_save,
            validate=validate_definition,
        ),
        Tool(
            "preview_app",
            "초안 판으로 기록된 정정을 다시 흘려, 켠 판과 알림이 어떻게 달라지는지 준다(비용 0).",
            preview_app,
            authorize=existing_owner,
            validate=validate_version,
        ),
        Tool(
            "enable_app",
            "앱의 판을 켠다. 구독자의 알림이 바뀌는 일이라 소유자의 확인을 받고 보류 시간 뒤에 켜진다.",
            enable_app,
            frozenset({"C"}),
            effect="external",
            confirm=True,
            authorize=existing_owner,
            validate=validate_version,
        ),
    ]
