"""판정 화면 데모용 가상 사업. 실제 공고가 아니다(제목에 "(가상)").

LLM 추출을 붙이기 전에는 실제 공고에 자격 규칙이 없다. 화면을 확인할 수 있게 원문 문단·자격 규칙·
마감일을 갖춘 가상 사업 셋을 DB에 넣는다.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from sqlalchemy.orm import Session

from noticedelta.db import models

DEMO = [
    {
        "key": "youth",
        "title": "(가상) 2026 부산 청년 창업 지원사업",
        "org": "(가상) 부산창업진흥원",
        "days_left": 21,
        "paragraphs": {
            "y1": "○ 신청 자격: 공고일 기준 창업 7년 이내 기업",
            "y2": "○ 대표자: 만 39세 이하",
            "y3": "○ 사업장 소재지: 부산광역시",
        },
        "rules": {
            "group": "all_of",
            "rules": [
                {
                    "id": "r1",
                    "attribute": "business_age_months",
                    "op": "<=",
                    "value": 84,
                    "source": "y1",
                },
                {"id": "r2", "attribute": "ceo_age", "op": "<=", "value": 39, "source": "y2"},
                {
                    "id": "r3",
                    "attribute": "region_sido",
                    "op": "==",
                    "value": "부산",
                    "source": "y3",
                },
            ],
        },
    },
    {
        "key": "voucher",
        "title": "(가상) 초기 창업 바우처",
        "org": "(가상) 창업지원재단",
        "days_left": 9,
        "paragraphs": {
            "v1": "○ 예비 창업자 또는 창업 3년 이내 기업",
            "v2": "○ 직전 연도 매출 10억 원 이하",
            "v3": "※ 같은 해 정부 창업 지원 사업을 받은 기업은 제외",
        },
        "rules": {
            "group": "all_of",
            "rules": [
                {
                    "group": "any_of",
                    "rules": [
                        {
                            "id": "r1",
                            "attribute": "is_pre_founder",
                            "op": "==",
                            "value": True,
                            "source": "v1",
                        },
                        {
                            "id": "r2",
                            "attribute": "business_age_months",
                            "op": "<=",
                            "value": 36,
                            "source": "v1",
                        },
                    ],
                },
                {
                    "id": "r3",
                    "attribute": "revenue_krw",
                    "op": "<=",
                    "value": 1_000_000_000,
                    "source": "v2",
                },
            ],
            "manual": [{"text": "같은 해 정부 창업 지원 중복 수혜 아님", "source": "v3"}],
        },
    },
    {
        "key": "hiring",
        "title": "(가상) 여성기업 고용 지원",
        "org": "(가상) 여성기업지원센터",
        "days_left": 45,
        "paragraphs": {
            "h1": "○ 여성기업확인서를 받은 기업",
            "h2": "○ 상시 근로자 5명 이상 50명 이하",
            "h3": "○ 재창업 기업은 제외",
        },
        "rules": {
            "group": "all_of",
            "rules": [
                {
                    "id": "r1",
                    "attribute": "is_women_owned",
                    "op": "==",
                    "value": True,
                    "source": "h1",
                },
                {
                    "id": "r2",
                    "attribute": "employees",
                    "op": "between",
                    "value": [5, 50],
                    "source": "h2",
                },
                {"id": "r3", "attribute": "is_restart", "op": "==", "value": False, "source": "h3"},
            ],
        },
    },
]


def seed_demo(session: Session, today: date) -> int:
    """가상 사업을 넣는다. 이미 있으면 건너뛴다. 넣은 사업 수를 준다."""
    if session.get(models.Source, "demo") is None:
        session.add(models.Source(id="demo", name="(가상) 데모"))
        session.flush()
    added = 0
    for d in DEMO:
        pid = f"demo-{d['key']}"
        if session.get(models.Program, pid) is not None:
            continue
        h = hashlib.sha256(pid.encode()).hexdigest()
        for row in [
            models.Program(
                id=pid, title=d["title"], title_key=pid, org=d["org"], current_version_no=1
            ),
            models.Notice(
                id=f"n-{d['key']}",
                source_id="demo",
                external_id=pid,
                title=d["title"],
                program_id=pid,
            ),
            models.NoticeVersion(
                id=f"nv-{d['key']}",
                notice_id=f"n-{d['key']}",
                version_no=1,
                kind="original",
                content_hash=h,
            ),
            models.ProgramVersion(
                id=f"pv-{d['key']}",
                program_id=pid,
                version_no=1,
                notice_version_id=f"nv-{d['key']}",
                content_hash=h,
            ),
        ]:
            session.add(row)
            session.flush()  # 관계 선언이 없어 외래 키 순서대로 넣는다
        for order, (lid, text) in enumerate(d["paragraphs"].items()):
            session.add(
                models.SegmentRow(
                    notice_version_id=f"nv-{d['key']}",
                    lineage_id=lid,
                    kind="paragraph",
                    ord=order,
                    text=text,
                    text_hash=hashlib.sha256(text.encode()).hexdigest(),
                    locator={"source": "body"},
                )
            )
        deadline = (today + timedelta(days=d["days_left"])).isoformat()
        for name, vtype, value, sources in [
            ("eligibility_rules", "rules", d["rules"], list(d["paragraphs"])),
            ("deadline", "datetime", deadline, []),
        ]:
            session.add(
                models.FieldValueRow(
                    program_id=pid,
                    program_version_id=f"pv-{d['key']}",
                    name=name,
                    value=value,
                    value_type=vtype,
                    source_lineage_ids=sources,
                    extractor="demo",
                )
            )
        added += 1
    session.flush()
    return added
