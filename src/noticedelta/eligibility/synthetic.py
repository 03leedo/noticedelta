"""대량 판정 측정용 가상 프로필과 가상 사업 규칙 (작업 목록 6번). 실제 사람·공고가 아니다."""

from __future__ import annotations

import random
from typing import Any

from noticedelta.eligibility.rules import SIDO, RuleGroup


def virtual_profiles(n: int, seed: int = 42, *, messy: float = 0.05) -> list[dict[str, Any]]:
    """속성 일부가 비어 있고, messy 비율만큼 폼에서 온 듯한 글자 값·잘못된 값이 섞인 프로필."""
    rng = random.Random(seed)

    def maybe(value, missing=0.08):
        return None if rng.random() < missing else value

    out = []
    for i in range(n):
        pre = rng.random() < 0.2
        p: dict[str, Any] = {
            "is_pre_founder": pre,
            "business_age_months": maybe(None if pre else rng.randint(0, 180)),
            "ceo_age": maybe(rng.randint(19, 70)),
            "region_sido": maybe(rng.choice(SIDO)),
            "business_type": maybe(rng.choice(["individual", "corporation"])),
            "revenue_krw": maybe(rng.choice([0, 5, 30, 80, 200, 1500]) * 10_000_000),
            "employees": maybe(rng.randint(0, 120)),
            "is_women_owned": maybe(rng.random() < 0.3),
            "is_restart": maybe(rng.random() < 0.1),
        }
        if rng.random() < messy:
            # 폼에서 온 글자 값과 읽을 수 없는 값
            k = rng.choice(
                ["business_age_months", "ceo_age", "employees", "revenue_krw", "is_women_owned"]
            )
            v = p[k]
            p[k] = rng.choice(
                [
                    f" {v} " if isinstance(v, int) and not isinstance(v, bool) else "예",
                    f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else "아니오",
                    "72개월",
                    True,
                    [1],
                ]
            )
        out.append({k: v for k, v in p.items() if v is not None} | {"user_id": f"u{i:06d}"})
    return out


SAMPLE_RULESETS: dict[str, RuleGroup] = {
    # (가상) 청년 창업 지원: 창업 7년 이내, 대표 만 39세 이하, 부산 소재
    "youth_startup": RuleGroup.model_validate(
        {
            "group": "all_of",
            "rules": [
                {"id": "r1", "attribute": "business_age_months", "op": "<=", "value": 84},
                {"id": "r2", "attribute": "ceo_age", "op": "<=", "value": 39},
                {"id": "r3", "attribute": "region_sido", "op": "==", "value": "부산"},
            ],
        }
    ),
    # (가상) 초기 창업 바우처: (예비 창업자 또는 업력 3년 이내) 그리고 매출 10억 이하, 중복 수혜 확인
    "early_voucher": RuleGroup.model_validate(
        {
            "group": "all_of",
            "rules": [
                {
                    "group": "any_of",
                    "rules": [
                        {"id": "r1", "attribute": "is_pre_founder", "op": "==", "value": True},
                        {"id": "r2", "attribute": "business_age_months", "op": "<=", "value": 36},
                    ],
                },
                {"id": "r3", "attribute": "revenue_krw", "op": "<=", "value": 1_000_000_000},
            ],
            "manual": [{"text": "같은 해 정부 창업 지원 중복 수혜 아님"}],
        }
    ),
    # (가상) 여성기업 고용 지원: 여성기업, 상시 근로자 5~50명, 재창업 제외, 업종은 신뢰도 낮음
    "women_hiring": RuleGroup.model_validate(
        {
            "group": "all_of",
            "rules": [
                {"id": "r1", "attribute": "is_women_owned", "op": "==", "value": True},
                {"id": "r2", "attribute": "employees", "op": "between", "value": [5, 50]},
                {"id": "r3", "attribute": "is_restart", "op": "==", "value": False},
                {
                    "id": "r4",
                    "attribute": "business_type",
                    "op": "in",
                    "value": ["corporation"],
                    "confidence": 0.5,
                },
            ],
        }
    ),
}
