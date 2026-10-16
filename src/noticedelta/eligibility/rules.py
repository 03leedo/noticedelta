"""자격 규칙 형식 (설계 7.6). 속성 사전에 없는 조건은 manual로 둔다."""

from __future__ import annotations

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
        return self


class ManualCondition(BaseModel):
    text: str
    source: str | None = None


class RuleGroup(BaseModel):
    group: Literal["all_of", "any_of"]
    rules: list[Rule | RuleGroup] = Field(default_factory=list)
    manual: list[ManualCondition] = Field(default_factory=list)
