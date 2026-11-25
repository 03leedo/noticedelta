"""앱 정의 규격 v0.1 (docs/app-spec.md 4·8장). 앱은 플랫폼이 돌리는 설정 한 장이다.

    load_app(path)              파일을 읽어 검증한다. 문제가 있으면 AppSpecError
    check_app(data, sources=)   검증만 하고 문제 목록을 돌려준다(에이전트가 고치며 다시 부른다)
    AppDefinition.model_json_schema()   에이전트가 읽는 JSON 스키마

문제마다 고칠 방법을 함께 적는다(3장: "알림의 fields에 amount2가 없다. 정의된 필드: ...").
엔진이 쓰는 모양(값 타입, 안내, 낱말, 속성 사전)으로 바꾸는 것도 여기서 한다. 엔진은 정의 파일을
모르고 이 모양만 받는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from noticedelta.eligibility.rules import Attribute, Derive
from noticedelta.eligibility.temporal import DERIVE

# 규격의 필드 타입 → 엔진의 값 타입(values.ValueType)
FIELD_TYPES: dict[str, str] = {
    "datetime": "datetime",
    "amount": "amount",
    "ruleset": "rules",
    "list": "list",
    "text": "text",
}
_ATTRIBUTE_TYPES: dict[str, type | None] = {
    "bool": bool,
    "int": int,
    "text": str,
    "enum": str,
    "date": None,
}
_DERIVE = re.compile(r"^(?P<fn>[a-z_]+)\((?P<src>[a-z_][a-z0-9_]*)\)$")
_NAME = r"^[a-z][a-z0-9_]{0,63}$"

# 한도 (8장). 넘으면 검증에서 막는다
MAX_SOURCES = 10
MAX_FIELDS = 30
MAX_KEYWORDS = 50
MAX_ATTRIBUTES = 60

Name = Annotated[str, Field(pattern=_NAME)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceUse(_Strict):
    id: Name
    read: list[str] = Field(min_length=1)
    attachments: bool = False


class MetaRef(_Strict):
    meta: str


class FieldSpec(_Strict):
    type: Literal["datetime", "amount", "ruleset", "list", "text"]
    from_: Literal["extract"] | MetaRef = Field(alias="from")
    else_: Literal["extract"] | None = Field(default=None, alias="else")
    guide: str | None = None  # LLM에 주는 안내. extract면 있어야 한다
    keywords: list[str] = []  # 정규식. 바뀐 조각이 이 필드와 관련 있을지 고른다
    note: str | None = None  # 사람이 읽는 메모. 엔진은 쓰지 않는다

    @property
    def extracts(self) -> bool:
        return self.from_ == "extract" or self.else_ == "extract"


class PromptSpec(_Strict):
    """추출 프롬프트의 앱별 부분(extraction.spec_for_app). 없으면 앱 제목을 분야로 쓴다."""

    domain: str = Field(min_length=1)  # "너는 {domain}에서 정해진 필드만 뽑는 추출기다"
    heading: str = "문서 조각"  # 입력에서 조각 목록 앞의 머리말
    notes: list[str] = []  # 규칙 속성의 단위 같은 참고 줄(규칙 필드가 있을 때)


class Engine(_Strict):
    kind: Literal["change_impact@1"]
    fields: dict[Name, FieldSpec] = Field(min_length=1)
    prompt: PromptSpec | None = None


class AttributeSpec(_Strict):
    type: Literal["bool", "int", "text", "enum", "date"]
    label: str
    values: list[str] = []
    derive: str | None = None  # "months_since(founded_on)": 플랫폼 파생 함수(이름)와 날짜 속성
    units: list[str] = []  # 글에서 숫자 뒤에 붙는 단위(인용 검증)
    scale: int | None = None  # 큰 단위로도 적는 배수: 개월은 12로 나눈 "년"으로도 적는다
    money: bool = False


class Subjects(_Strict):
    model: Literal["rules", "watchlist", "everyone"]
    rules_field: Name | None = None
    attributes: dict[Name, AttributeSpec] = {}


class AlertRule(_Strict):
    on: Literal["verdict_changed", "field_changed"]
    fields: list[str] = []
    to: Literal["subscribers", "matched", "affected"] | None = None
    urgent: list[str] = []


class Hold(_Strict):
    mass_impact: int = Field(ge=1, le=100_000)
    min_confidence: float = Field(ge=0.0, le=1.0)


class Alerts(_Strict):
    rules: list[AlertRule] = Field(min_length=1)
    channels: list[Literal["inbox", "email", "calendar", "webhook"]] = Field(min_length=1)
    hold: Hold


class Budget(_Strict):
    llm_usd_per_month: float = Field(ge=0)
    on_exceed: Literal["pause_extraction"]


class Labels(_Strict):
    fields: dict[str, str] = {}
    results: dict[str, str] = {}


class AppDefinition(_Strict):
    kind: Literal["app"]
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=100)
    owner: str
    sensitivity: Literal["public", "personal", "sensitive"]
    sources: list[SourceUse] = Field(min_length=1)
    engine: Engine
    subjects: Subjects
    alerts: Alerts
    budget: Budget
    labels: Labels = Labels()

    # --- 엔진이 쓰는 모양 ---

    def value_types(self) -> dict[str, str]:
        return {n: FIELD_TYPES[f.type] for n, f in self.engine.fields.items()}

    def guides(self) -> dict[str, str]:
        return {n: f.guide for n, f in self.engine.fields.items() if f.extracts and f.guide}

    def keyword_patterns(self) -> dict[str, str]:
        return {n: "|".join(f.keywords) for n, f in self.engine.fields.items() if f.keywords}

    def reads_change(
        self, source_id: str, keys: Iterable[str] | None, attachments: bool = False
    ) -> bool:
        """이 앱에게 변경인가 (app-spec 9장). 출처 단위의 판 하나를 여러 앱이 나눠 읽는다.

        바뀐 키(`keys`)가 앱이 읽는 키와 겹치거나, 첨부를 읽는 앱이고 첨부가 바뀌었으면 변경이다.
        어떤 키가 바뀌었는지 모르면(`None`, 예전 기록·출처가 바뀐 판) 받는다. 놓치는 것보다 낫다.
        """
        use = next((u for u in self.sources if u.id == source_id), None)
        if use is None:
            return False
        if attachments and use.attachments:
            return True
        return keys is None or not set(use.read).isdisjoint(keys)

    def alert_fields(self) -> set[str]:
        """구독자에게 값 변경을 알리는 필드(alerts.rules의 field_changed). 나머지 필드의 변경은 알리지 않는다."""
        return {f for r in self.alerts.rules if r.on == "field_changed" for f in r.fields}

    def alerts_on_verdict(self) -> bool:
        """판정이 바뀐 사람에게 알리는가(alerts.rules의 verdict_changed)."""
        return any(r.on == "verdict_changed" for r in self.alerts.rules)

    def meta_fields(self) -> tuple[str, ...]:
        """출처가 구조화해 준 값에서 읽는 필드."""
        return tuple(n for n, f in self.engine.fields.items() if isinstance(f.from_, MetaRef))

    def rule_attributes(self) -> dict[str, Attribute]:
        """규칙이 다루는 속성 사전. 날짜 속성은 파생 속성의 재료일 뿐 규칙에 쓰지 않는다."""
        out: dict[str, Attribute] = {}
        for name, a in self.subjects.attributes.items():
            if a.type == "date":
                continue
            derive = None
            if a.derive and (m := _DERIVE.match(a.derive)):
                derive = Derive(m["fn"], m["src"])
            out[name] = Attribute(
                _ATTRIBUTE_TYPES[a.type],
                units=tuple(a.units),
                scale=a.scale,
                money=a.money,
                derive=derive,
                values=tuple(a.values),
            )
        return out

    def content_hash(self) -> str:
        """판과 함께 쓰는 내용 해시(9장). 같은 판 번호로 내용이 바뀌면 드러난다."""
        body = self.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    fix: str = ""

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" + (f" ({self.fix})" if self.fix else "")


class AppSpecError(ValueError):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("앱 정의에 문제가 있다:\n" + "\n".join(f"- {p}" for p in problems))


def _names(xs) -> str:
    return ", ".join(sorted(xs)) or "(없음)"


def _shape_problems(e: ValidationError) -> list[Problem]:
    out = []
    for err in e.errors():
        path = ".".join(str(x) for x in err["loc"]) or "(맨 위)"
        fix = ""
        if err["type"] == "extra_forbidden":
            fix = "규격에 없는 칸이다. 이름을 확인하거나 지운다"
        elif err["type"] == "missing":
            fix = "꼭 있어야 하는 칸이다"
        elif err["type"] == "literal_error":
            fix = f"고를 수 있는 값: {err.get('ctx', {}).get('expected', '')}"
        out.append(Problem(path, err["msg"], fix))
    return out


def _meaning_problems(
    app: AppDefinition,
    sources: dict[str, set[str] | None] | None,
    meta: dict[str, set[str]] | None = None,
) -> list[Problem]:
    p: list[Problem] = []
    fields = app.engine.fields
    attrs = app.subjects.attributes

    if len(app.sources) > MAX_SOURCES:
        p.append(Problem("sources", f"출처가 {len(app.sources)}개다", f"{MAX_SOURCES}개까지"))
    seen: set[str] = set()
    for i, s in enumerate(app.sources):
        if s.id in seen:
            p.append(Problem(f"sources.{i}.id", f"출처 {s.id}가 두 번 있다", "하나로 합친다"))
        seen.add(s.id)
        if sources is None:
            continue
        if s.id not in sources:
            p.append(
                Problem(
                    f"sources.{i}.id",
                    f"카탈로그에 없는 출처 {s.id}",
                    f"있는 출처: {_names(sources)}",
                )
            )
            continue
        keys = sources[s.id]
        if keys is not None and (unknown := set(s.read) - keys):
            p.append(
                Problem(
                    f"sources.{i}.read",
                    f"{s.id}가 내지 않는 키: {_names(unknown)}",
                    f"{s.id}의 키: {_names(keys)}",
                )
            )

    if len(fields) > MAX_FIELDS:
        p.append(Problem("engine.fields", f"필드가 {len(fields)}개다", f"{MAX_FIELDS}개까지"))
    for name, f in fields.items():
        at = f"engine.fields.{name}"
        if f.extracts and not (f.guide or "").strip():
            p.append(
                Problem(
                    f"{at}.guide", "글에서 뽑는 필드에 안내가 없다", "LLM에 줄 한 줄 설명을 적는다"
                )
            )
        if isinstance(f.from_, MetaRef) and f.else_ is None and f.keywords:
            p.append(
                Problem(
                    f"{at}.keywords",
                    "메타에서만 읽는 필드에 낱말이 있다",
                    "else: extract를 두거나 낱말을 지운다",
                )
            )
        if len(f.keywords) > MAX_KEYWORDS:
            p.append(
                Problem(f"{at}.keywords", f"낱말이 {len(f.keywords)}개다", f"{MAX_KEYWORDS}개까지")
            )
        for j, kw in enumerate(f.keywords):
            try:
                re.compile(kw)
            except re.error as e:
                p.append(
                    Problem(
                        f"{at}.keywords.{j}", f"정규식이 아니다: {e}", "괄호와 역슬래시를 확인한다"
                    )
                )

    if meta is not None:
        for name, f in fields.items():
            if not isinstance(f.from_, MetaRef):
                continue
            missing = [s.id for s in app.sources if f.from_.meta not in meta.get(s.id, set())]
            if missing and f.else_ is None:
                offered = sorted(set().union(*(meta.get(s.id, set()) for s in app.sources)))
                p.append(
                    Problem(
                        f"engine.fields.{name}.from",
                        f"{', '.join(missing)}가 구조화 값 {f.from_.meta}를 주지 않는다",
                        f"else: extract를 두거나 고른다. 주는 값: {', '.join(offered) or '(없음)'}",
                    )
                )

    sub = app.subjects
    if sub.model == "rules":
        if sub.rules_field is None:
            p.append(
                Problem(
                    "subjects.rules_field",
                    "rules 모델에 규칙 필드가 없다",
                    "ruleset 타입 필드 이름을 적는다",
                )
            )
        elif sub.rules_field not in fields:
            p.append(
                Problem(
                    "subjects.rules_field",
                    f"없는 필드 {sub.rules_field}",
                    f"정의된 필드: {_names(fields)}",
                )
            )
        elif fields[sub.rules_field].type != "ruleset":
            p.append(
                Problem(
                    "subjects.rules_field",
                    f"{sub.rules_field}는 ruleset이 아니다",
                    "type: ruleset인 필드를 고른다",
                )
            )
        if not attrs:
            p.append(
                Problem(
                    "subjects.attributes",
                    "rules 모델에 속성 사전이 없다",
                    "규칙이 쓰는 속성을 적는다",
                )
            )
    if len(attrs) > MAX_ATTRIBUTES:
        p.append(
            Problem("subjects.attributes", f"속성이 {len(attrs)}개다", f"{MAX_ATTRIBUTES}개까지")
        )
    for name, a in attrs.items():
        at = f"subjects.attributes.{name}"
        if a.type == "enum" and not a.values:
            p.append(Problem(f"{at}.values", "enum인데 고를 값이 없다", "values에 값을 적는다"))
        if a.derive is None:
            continue
        m = _DERIVE.match(a.derive)
        if m is None:
            p.append(
                Problem(
                    f"{at}.derive",
                    f"모양이 틀렸다: {a.derive}",
                    "함수(날짜 속성) 꼴로 적는다. 예: age(birth_date)",
                )
            )
        elif m["fn"] not in DERIVE:
            p.append(
                Problem(f"{at}.derive", f"없는 파생 함수 {m['fn']}", f"있는 함수: {_names(DERIVE)}")
            )
        elif attrs.get(m["src"]) is None or attrs[m["src"]].type != "date":
            p.append(
                Problem(
                    f"{at}.derive",
                    f"{m['src']}는 date 속성이 아니다",
                    "type: date인 속성을 재료로 둔다",
                )
            )
        elif a.type != "int":
            p.append(Problem(f"{at}.type", "파생 속성은 int다", "type: int로 둔다"))

    results = set(app.labels.results) or {"eligible", "ineligible", "unknown"}
    for i, r in enumerate(app.alerts.rules):
        at = f"alerts.rules.{i}"
        if unknown := set(r.fields) - set(fields):
            p.append(
                Problem(
                    f"{at}.fields",
                    f"없는 필드: {_names(unknown)}",
                    f"정의된 필드: {_names(fields)}",
                )
            )
        if r.on == "verdict_changed":
            if sub.model != "rules":
                p.append(
                    Problem(f"{at}.on", "판정은 rules 모델에서만 바뀐다", "field_changed를 쓴다")
                )
            for u in r.urgent:
                before, _, after = u.partition("→")
                if not after or {before, after} - results:
                    p.append(
                        Problem(
                            f"{at}.urgent",
                            f"판정 변화가 아니다: {u}",
                            f"'전→후' 꼴, 판정: {_names(results)}",
                        )
                    )
        elif unknown := set(r.urgent) - set(r.fields):
            p.append(
                Problem(
                    f"{at}.urgent",
                    f"이 규칙의 fields에 없는 필드: {_names(unknown)}",
                    "fields에 먼저 넣는다",
                )
            )
    if "webhook" in app.alerts.channels:
        p.append(
            Problem(
                "alerts.channels",
                "밖의 주소로 보내는 채널은 승인 기록이 있어야 한다",
                "도구 게이트웨이가 생길 때까지 inbox·email·calendar만",
            )
        )

    if app.sensitivity == "sensitive":
        p.append(
            Problem(
                "sensitivity",
                "sensitive 앱은 외부 모델로 보내지 않는다",
                "로컬 모델이 생기기 전까지 켤 수 없다",
            )
        )
    if unknown := set(app.labels.fields) - set(fields):
        p.append(
            Problem(
                "labels.fields",
                f"없는 필드의 이름: {_names(unknown)}",
                f"정의된 필드: {_names(fields)}",
            )
        )
    return p


def check_app(
    data: dict,
    *,
    sources: dict[str, set[str] | None] | None = None,
    meta: dict[str, set[str]] | None = None,
) -> tuple[AppDefinition | None, list[Problem]]:
    """정의를 검증한다. sources를 주면 출처와 키도 본다({출처 id: 내는 키, 모르면 None})."""
    try:
        app = AppDefinition.model_validate(data)
    except ValidationError as e:
        return None, _shape_problems(e)
    problems = _meaning_problems(app, sources, meta)
    return (None if problems else app), problems


def load_app(
    path: Path | str,
    *,
    sources: dict[str, set[str] | None] | None = None,
    meta: dict[str, set[str]] | None = None,
) -> AppDefinition:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    app, problems = check_app(data, sources=sources, meta=meta)
    if app is None:
        raise AppSpecError(problems)
    return app
