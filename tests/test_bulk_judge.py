"""대량 판정 SQL이 judge()와 같은 결과·근거를 내는지 (작업 목록 6번)."""

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db import make_engine, models
from noticedelta.eligibility import RuleGroup, bulk_judge, judge, ruleset_hash, tri_state
from noticedelta.eligibility.sql import result_label
from noticedelta.eligibility.synthetic import SAMPLE_RULESETS, virtual_profiles

EDGE_GROUPS = {
    "empty_all": RuleGroup(group="all_of"),
    "empty_any_manual": RuleGroup.model_validate(
        {"group": "any_of", "manual": [{"text": "직접 확인"}]}
    ),
    "nested_any_with_empty": RuleGroup.model_validate(
        {
            "group": "any_of",
            "rules": [
                {"group": "all_of"},
                {"id": "a", "attribute": "ceo_age", "op": ">", "value": 60},
                {"id": "b", "attribute": "region_sido", "op": "not_in", "value": ["서울", "경기"]},
            ],
        }
    ),
    "neq_and_between": RuleGroup.model_validate(
        {
            "group": "all_of",
            "rules": [
                {"id": "a", "attribute": "business_type", "op": "!=", "value": "individual"},
                {"id": "b", "attribute": "employees", "op": "between", "value": [1, 9]},
                {"id": "c", "attribute": "ceo_age", "op": ">=", "value": 30},
            ],
        }
    ),
}
GROUPS = SAMPLE_RULESETS | EDGE_GROUPS
TYPED = ["business_age_months", "ceo_age", "employees", "revenue_krw"]


def test_tri_state_matches_judge_on_typed_columns():
    """SQLite에서도 도는 3상태 논리 검사. 값이 깔끔한 열(int/str/bool)에 대해."""
    engine = sa.create_engine("sqlite://")
    md = sa.MetaData()
    t = sa.Table(
        "p",
        md,
        sa.Column("user_id", sa.String, primary_key=True),
        *[sa.Column(a, sa.Integer) for a in TYPED],
        *[sa.Column(a, sa.String) for a in ["region_sido", "business_type"]],
        *[sa.Column(a, sa.Boolean) for a in ["is_pre_founder", "is_women_owned", "is_restart"]],
    )
    md.create_all(engine)
    profiles = virtual_profiles(400, seed=7, messy=0)
    with engine.begin() as conn:
        conn.execute(t.insert(), [{c.name: p.get(c.name) for c in t.c} for p in profiles])
        for name, g in GROUPS.items():
            label = result_label(tri_state(g, lambda a: t.c[a]))
            got = dict(conn.execute(sa.select(t.c.user_id, label)).all())
            want = {p["user_id"]: judge(g, p).result for p in profiles}
            assert got == want, name


def test_ruleset_hash_changes_with_rules():
    a, b = SAMPLE_RULESETS["youth_startup"], SAMPLE_RULESETS["early_voucher"]
    assert ruleset_hash(a) == ruleset_hash(a.model_copy(deep=True)) != ruleset_hash(b)


def test_bool_is_not_read_as_a_number():
    g = SAMPLE_RULESETS["youth_startup"]
    assert judge(g, {"business_age_months": True, "ceo_age": 30, "region_sido": "부산"}).result == (
        "unknown"
    )


# --- PostgreSQL (NOTICEDELTA_PG_URL이 있을 때만) ---

PG_URL = os.environ.get("NOTICEDELTA_PG_URL")
pg = pytest.mark.skipif(not PG_URL, reason="NOTICEDELTA_PG_URL 없음")
SCHEMA = "test_bulk_judge"


@pytest.fixture
def pg_session():
    with sa.create_engine(PG_URL).begin() as conn:
        conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}"))
    engine = make_engine(PG_URL, connect_args={"options": f"-csearch_path={SCHEMA}"})
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def seed_program(s):
    for row in [
        models.Source(id="bizinfo", name="기업마당"),
        models.Program(id="PG1", title="(가상) 사업", title_key="가상사업"),
        models.Notice(id="N1", source_id="bizinfo", external_id="X", title="(가상)"),
        models.NoticeVersion(
            id="NV1", notice_id="N1", version_no=1, kind="original", content_hash="h"
        ),
        models.ProgramVersion(
            id="PV1", program_id="PG1", version_no=1, notice_version_id="NV1", content_hash="h"
        ),
    ]:
        s.add(row)
        s.flush()  # 관계 선언이 없어 외래 키 순서대로 하나씩 넣는다


@pg
def test_bulk_judge_matches_judge_with_messy_profiles(pg_session):
    s = pg_session
    seed_program(s)
    profiles = virtual_profiles(1500, seed=3, messy=0.3)
    s.add_all(
        models.Profile(
            user_id=p["user_id"], attributes={k: v for k, v in p.items() if k != "user_id"}
        )
        for p in profiles
    )
    s.flush()
    for name, g in GROUPS.items():
        n = bulk_judge(s, program_id="PG1", program_version_id="PV1", group=g)
        assert n == len(profiles), name
        rows = s.execute(
            sa.select(
                models.JudgmentRow.user_id, models.JudgmentRow.result, models.JudgmentRow.reasons
            ).where(models.JudgmentRow.ruleset_hash == ruleset_hash(g))
        ).all()
        by_user = {p["user_id"]: p for p in profiles}
        for user_id, result, reasons in rows:
            want = judge(g, by_user[user_id])
            assert result == want.result, (name, by_user[user_id])
            assert [r["outcome"] for r in reasons] == [r.outcome for r in want.reasons], name
            assert [r["rule_id"] for r in reasons] == [r.rule_id for r in want.reasons], name
        # 다시 돌려도 쌓이지 않는다
        assert bulk_judge(s, program_id="PG1", program_version_id="PV1", group=g) == 0
