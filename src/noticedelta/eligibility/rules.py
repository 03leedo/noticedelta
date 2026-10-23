"""자격 규칙 형식 (설계 7.6). 속성 사전에 없는 조건은 manual로 둔다."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Op = Literal["==", "!=", "<", "<=", ">", ">=", "in", "not_in", "between"]

ATTRIBUTES: dict[str, type] = {
    "is_pre_founder": bool,
    "business_age_months": int,
    "ceo_age": int,
    "region_sido": str,
    "industry_ksic": str,
    "business_type": str,  # individual / corporation
    "revenue_krw": int,
    "employees": int,
    "is_women_owned": bool,
    "is_restart": bool,
}


def _type_ok(v: Any, expected: type) -> bool:
    if expected is bool:
        return isinstance(v, bool)
    if expected is int:
        return isinstance(v, int | float) and not isinstance(v, bool)
    return isinstance(v, expected)


class Rule(BaseModel):
    id: str = ""
    attribute: str
    op: Op
    value: Any
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("attribute")
    @classmethod
    def _known_attribute(cls, v: str) -> str:
        if v not in ATTRIBUTES:
            raise ValueError(f"속성 사전에 없는 속성: {v}")
        return v

    @model_validator(mode="after")
    def _value_shape(self) -> Rule:
        if self.op in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError(f"{self.op} 연산자는 목록 값이 필요하다")
        if self.op == "between" and not (isinstance(self.value, list) and len(self.value) == 2):
            raise ValueError("between 연산자는 [하한, 상한] 값이 필요하다")
        # 값 타입을 속성 사전과 맞춘다. "abc" 같은 값 하나가 대량 판정 SQL 전체를 깨뜨리지 않게.
        expected = ATTRIBUTES[self.attribute]
        for v in self.value if isinstance(self.value, list) else [self.value]:
            if not _type_ok(v, expected):
                raise ValueError(f"{self.attribute} 값은 {expected.__name__}이어야 한다: {v!r}")
        return self


class ManualCondition(BaseModel):
    text: str
    source: str | None = None


class RuleGroup(BaseModel):
    group: Literal["all_of", "any_of"]
    rules: list[Rule | RuleGroup] = Field(default_factory=list)
    manual: list[ManualCondition] = Field(default_factory=list)


def ruleset_hash(group: RuleGroup) -> str:
    """규칙 묶음의 내용 해시. 같은 규칙으로 같은 사용자를 두 번 판정하지 않게 한다."""
    return hashlib.sha256(group.model_dump_json().encode()).hexdigest()
