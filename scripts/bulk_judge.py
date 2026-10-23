"""가상 프로필 대량 판정 측정 (작업 목록 6번). PostgreSQL 전용, bench 스키마를 새로 만든다.

    docker compose up -d postgres
    uv run python scripts/bulk_judge.py [--n 10000]

가상 사업 규칙 3개로 프로필 전체를 SQL 한 문장씩 판정해 judgment에 쌓고, 같은 일을 파이썬 판정기로
한 결과와 하나하나 맞춰 본다. 출력은 건수·시간·일치율과 판정 근거 예시 하나다.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_samples import load_env  # noqa: E402

from noticedelta.db import make_engine, models  # noqa: E402
from noticedelta.eligibility import bulk_judge, judge, ruleset_hash  # noqa: E402
from noticedelta.eligibility.synthetic import SAMPLE_RULESETS, virtual_profiles  # noqa: E402

SCHEMA = "bench"
DEFAULT_URL = "postgresql+psycopg://noticedelta:noticedelta@localhost:5433/noticedelta"


def setup(url: str, n: int) -> sa.Engine:
    with sa.create_engine(url).begin() as conn:
        conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}"))
    engine = make_engine(url, connect_args={"options": f"-csearch_path={SCHEMA}"})
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.Source(id="bizinfo", name="기업마당"))
        s.flush()
        for i, name in enumerate(SAMPLE_RULESETS):
            s.add(models.Program(id=f"PG{i}", title=f"(가상) {name}", title_key=name))
            s.add(models.Notice(id=f"N{i}", source_id="bizinfo", external_id=name, title=name))
            s.flush()
            s.add(
                models.NoticeVersion(
                    id=f"NV{i}", notice_id=f"N{i}", version_no=1, kind="original", content_hash=name
                )
            )
            s.flush()
            for no, pv in [(1, f"PV{i}"), (2, f"PY{i}")]:  # PY: 파이썬 경로 비교용
                s.add(
                    models.ProgramVersion(
                        id=pv,
                        program_id=f"PG{i}",
                        version_no=no,
                        notice_version_id=f"NV{i}",
                        content_hash=f"{name}{no}",
                    )
                )
        s.flush()
        s.execute(
            sa.insert(models.Profile),
            [{"user_id": p.pop("user_id"), "attributes": p} for p in virtual_profiles(n, seed=42)],
        )
        s.commit()
    return engine


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10_000)
    args = ap.parse_args()
    load_env(ROOT / ".env")
    url = os.environ.get("NOTICEDELTA_PG_URL", DEFAULT_URL)
    engine = setup(url, args.n)
    print(f"가상 프로필 {args.n:,}명, 가상 사업 {len(SAMPLE_RULESETS)}개")

    with Session(engine) as s:
        for i, (name, group) in enumerate(SAMPLE_RULESETS.items()):
            t0 = time.perf_counter()
            n = bulk_judge(s, program_id=f"PG{i}", program_version_id=f"PV{i}", group=group)
            s.commit()
            sql_s = time.perf_counter() - t0

            # 같은 일을 파이썬으로: 프로필 읽기 → 판정 → 판정 기록 쓰기
            t0 = time.perf_counter()
            loaded = s.execute(sa.select(models.Profile.user_id, models.Profile.attributes)).all()
            want = {u: judge(group, a) for u, a in loaded}
            h = ruleset_hash(group)
            s.execute(
                sa.insert(models.JudgmentRow),
                [
                    {
                        "user_id": u,
                        "program_id": f"PG{i}",
                        "program_version_id": f"PY{i}",
                        "ruleset_hash": h,
                        "result": j.result,
                        "reasons": [vars(r) for r in j.reasons],
                    }
                    for u, j in want.items()
                ],
            )
            s.commit()
            py_s = time.perf_counter() - t0

            rows = s.execute(
                sa.select(
                    models.JudgmentRow.user_id,
                    models.JudgmentRow.result,
                    models.JudgmentRow.reasons,
                ).where(models.JudgmentRow.program_version_id == f"PV{i}")
            ).all()
            same = sum(
                r == want[u].result
                and [x["outcome"] for x in rs] == [x.outcome for x in want[u].reasons]
                for u, r, rs in rows
            )
            dist = Counter(r for _, r, _ in rows)
            print(
                f"- {name}: {n:,}건 SQL 한 문장 {sql_s * 1000:.0f}ms, 파이썬(읽기·판정·쓰기) "
                f"{py_s * 1000:.0f}ms, 결과·근거 일치 {same}/{len(rows)}, "
                f"해당 {dist['eligible']:,} 비해당 {dist['ineligible']:,} 확인 필요 {dist['unknown']:,}"
            )
            example = next((rs for _, r, rs in rows if r == "unknown"), None)
            if example and i == 0:
                print("  확인 필요 예시 근거:")
                for x in example:
                    print(f"    [{x['outcome']}] {x['detail']} (프로필 값: {x['actual']!r})")


if __name__ == "__main__":
    main()
