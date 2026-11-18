"""규칙 형식 (설계 7.6). 속성 사전에 없는 조건은 manual로 둔다.

속성 사전({이름: Attribute})은 도메인 팩이 넘긴다(지원사업은 grants.ATTRIBUTES). 규칙을 검증할 때
context={"attributes": 사전}을 주면 속성 이름과 값 타입까지 검사한다. 믿을 수 없는 입력(LLM 출력)은
반드시 사전을 주고 검사한다. 사전 없이 읽는 것은 이미 검사해 저장한 규칙이다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

Op = Literal["==", "!=", "<", "<=", ">", ">=", "in", "not_in", "between"]


@dataclass(frozen=True)
class Derive:
    """다른 속성(날짜)에서 기준일마다 계산하는 속성. fn은 temporal.DERIVE에 있는 이름이다."""

    fn: Literal["months_since", "age"]
    source: str  # 프로필의 날짜 키


@dataclass(frozen=True)
class Attribute:
    """규칙이 다루는 속성 하나."""

    type: type  # bool | int | str
    units: tuple[str, ...] = ()  # 글에서 숫자 뒤에 붙는 단위(인용 검증): ("세",)
    scale: int | None = None  # 큰 단위로도 적는 배수: 개월 속성은 12로 나눈 "년"으로도 적는다
    money: bool = False  # 금액(원)으로 적는다: "3억원"
    derive: Derive | None = None
    values: tuple[str, ...] = ()  # 고를 수 있는 값(화면의 선택지)


Attributes = Mapping[str, Attribute]


def _attributes(info: ValidationInfo | None) -> Attributes | None:
    return (info.context or {}).get("attributes") if info is not None else None


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
    def _known_attribute(cls, v: str, info: ValidationInfo) -> str:
        attributes = _attributes(info)
        if attributes is not None and v not in attributes:
            raise ValueError(f"속성 사전에 없는 속성: {v}")
        return v

    @model_validator(mode="after")
    def _value_shape(self, info: ValidationInfo) -> Rule:
        if self.op in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError(f"{self.op} 연산자는 목록 값이 필요하다")
        if self.op == "between" and not (isinstance(self.value, list) and len(self.value) == 2):
            raise ValueError("between 연산자는 [하한, 상한] 값이 필요하다")
        # 값 타입을 속성 사전과 맞춘다. "abc" 같은 값 하나가 대량 판정 SQL 전체를 깨뜨리지 않게.
        attributes = _attributes(info)
        if attributes is None:
            return self
        expected = attributes[self.attribute].type
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
