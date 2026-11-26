"""사람의 정보를 쓰는 도구(B): 내 정보, 자격 확인, 근거 설명 (platform-services 5.8).

게이트웨이(tool_gateway)로만 부른다. 도구는 금고를 직접 열지 않고, 게이트웨이가 금고에서 `assist`
용도로 꺼낸 값을 profile 인자로 받는다. 동의하지 않은 키는 profile에 없다.

- get_profile: 동의한 내 정보(B).
- check(item_id): 항목의 지금 규칙으로 판정한 결과와 이유(A·B). 규칙의 값은 외부 글에서 뽑은 것이다.
- explain(item_id): check에 더해 규칙의 근거 조각(원문 발췌), 날짜 필드(마감 등), 다음에 판정이
  바뀌는 날(A·B).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.appspec import AppDefinition
from noticedelta.db.models import FieldValueRow, Item, ItemVersion, SegmentRow
from noticedelta.eligibility import RuleGroup, derive_profile, eligibility_timeline, judge
from noticedelta.tooling import Tool

PURPOSE = "assist"
MAX_SOURCE = 400  # 근거 조각 하나의 글자 수


def _clip(text: str | None, n: int = MAX_SOURCE) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[:n] + "…"


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def person_tools(
    session_factory: Callable[[], Session],
    app: AppDefinition,
    *,
    today: Callable[[], date] = date.today,
) -> list[Tool]:
    rules_field = app.subjects.rules_field
    attributes = app.rule_attributes()
    labels = {n: a.label for n, a in app.subjects.attributes.items()}
    keys = tuple(sorted(app.subjects.attributes))
    date_fields = [n for n, f in app.engine.fields.items() if f.type == "datetime"]

    def current(s: Session, item_id: str) -> tuple[ItemVersion, dict[str, FieldValueRow]] | None:
        """규칙을 뽑아 둔 가장 새 판과 그 판의 필드."""
        pv = s.scalar(
            sa.select(ItemVersion)
            .join(FieldValueRow, FieldValueRow.item_version_id == ItemVersion.id)
            .where(
                ItemVersion.item_id == item_id,
                FieldValueRow.app_id == app.id,
                FieldValueRow.name == rules_field,
            )
            .order_by(ItemVersion.version_no.desc())
            .limit(1)
        )
        if pv is None:
            return None
        rows = s.scalars(
            sa.select(FieldValueRow).where(
                FieldValueRow.item_version_id == pv.id, FieldValueRow.app_id == app.id
            )
        ).all()
        return pv, {r.name: r for r in rows}

    def verdict(s: Session, item_id: str, profile: dict) -> dict:
        item = s.get(Item, item_id)
        if item is None:
            return {"error": f"없는 항목: {item_id}"}
        found = current(s, item_id) if rules_field else None
        if found is None:
            return {"item_id": item_id, "title": item.title, "error": "아직 규칙을 뽑지 않은 항목"}
        pv, fields = found
        rules = RuleGroup.model_validate(fields[rules_field].value)
        on = today()
        j = judge(rules, derive_profile(profile, on, attributes))
        return {
            "item_id": item_id,
            "title": item.title,
            "version": pv.version_no,
            "on": on.isoformat(),
            "result": j.result,
            "reasons": [
                {"rule": r.rule_id, "outcome": r.outcome, "detail": r.detail, "source": r.source}
                for r in j.reasons
            ],
            "missing": sorted(set(keys) - set(profile)),  # 넣지 않았거나 동의하지 않은 정보
            "_pv": pv,
            "_fields": fields,
            "_rules": rules,
        }

    def public(out: dict) -> dict:
        return {k: v for k, v in out.items() if not k.startswith("_")}

    def get_profile(profile: dict) -> dict:
        """동의한 내 정보를 준다. 동의하지 않은 정보는 없다."""
        return {
            "profile": {labels.get(k, k): v for k, v in sorted(profile.items())},
            "keys": sorted(profile),
        }

    def check(item_id: str, profile: dict) -> dict:
        """항목 하나에 내가 해당하는지, 이유와 함께 준다."""
        with session_factory() as s:
            return public(verdict(s, item_id, profile))

    def explain(item_id: str, profile: dict) -> dict:
        """check에 더해 규칙의 근거 조각(원문 발췌), 마감, 판정이 다음에 바뀌는 날을 준다."""
        with session_factory() as s:
            out = verdict(s, item_id, profile)
            if "_pv" not in out:
                return public(out)
            pv, fields, rules = out["_pv"], out["_fields"], out["_rules"]
            lids = set(fields[rules_field].source_lineage_ids) | {
                r["source"] for r in out["reasons"] if r["source"]
            }
            texts = (
                dict(
                    s.execute(
                        sa.select(SegmentRow.lineage_id, SegmentRow.text).where(
                            SegmentRow.notice_version_id == pv.notice_version_id,
                            SegmentRow.lineage_id.in_(lids),
                        )
                    ).all()
                )
                if lids
                else {}
            )
            # 앱의 날짜 필드(마감 등) 가운데 오늘 이후로 가장 이른 날까지 판정이 바뀌는 날을 찾는다
            on = today()
            dates = {
                n: d
                for n in date_fields
                if n in fields and (d := _as_date(fields[n].value)) is not None
            }
            until = min((d for d in dates.values() if d >= on), default=None)
            timeline = eligibility_timeline(
                rules, profile, attributes=attributes, start=on, until=until
            )
            nxt = timeline.next_change
            out["sources"] = {k: _clip(v) for k, v in sorted(texts.items())}
            out["dates"] = {k: v.isoformat() for k, v in sorted(dates.items())}
            out["next_change"] = (
                {"on": nxt.on.isoformat(), "before": nxt.before, "after": nxt.after}
                if nxt is not None
                else None
            )
            return public(out)

    b = {"purpose": PURPOSE, "keys": keys}
    return [
        Tool(
            "get_profile",
            "동의한 내 정보(자격 판정에 쓰는 값)를 준다.",
            get_profile,
            frozenset({"B"}),
            **b,
        ),
        Tool(
            "check",
            "항목 하나에 내가 해당하는지(해당·해당 안 됨·모름)와 이유를 준다.",
            check,
            frozenset({"A", "B"}),
            logged_args=("item_id",),
            **b,
        ),
        Tool(
            "explain",
            "check에 더해 규칙의 근거가 된 원문 발췌, 마감, 판정이 다음에 바뀌는 날을 준다.",
            explain,
            frozenset({"A", "B"}),
            logged_args=("item_id",),
            **b,
        ),
    ]
