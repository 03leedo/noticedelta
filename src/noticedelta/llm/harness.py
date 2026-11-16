"""하네스: LLM 호출 앞뒤에 끼우는 단계 (ARCHITECTURE 3.2).

게이트웨이가 요청을 보내기 전에 before를 순서대로, 응답을 받은 뒤 after를 거꾸로 부른다.

    PIIMask         개인정보를 자리표시로 바꿔 보내고, 응답에서 원래 값으로 되돌린다
    InjectionGuard  입력에 숨은 지시로 보이는 줄을 찾아 가리거나(기본) 호출을 막는다

녹화본의 키와 내용은 하네스를 거친 요청으로 만든다. 그래서 가린 값은 녹화본에도 남지 않는다.
규칙 기반 1차 구현이다. 사람 이름은 가리지 못하고, 인젝션 탐지는 표현을 바꾸면 피할 수 있다.
구조적인 방어는 능력 분리(추출 LLM에는 도구도 민감 데이터도 없다)이고, 이것은 그 위의 한 겹이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # 게이트웨이가 이 모듈을 가져다 쓴다. 타입 표시에만 필요하다
    from noticedelta.llm.gateway import LLMRequest, LLMResponse


@dataclass(frozen=True)
class Finding:
    harness: str  # "pii" | "injection"
    kind: str  # 개인정보 종류 또는 규칙 이름
    count: int = 1
    sample: str = ""  # 운영자가 볼 짧은 발췌. 개인정보는 남기지 않는다


@dataclass
class Trace:
    """호출 한 번 동안 하네스가 남기는 기록. after 단계가 before 단계의 상태를 여기서 찾는다."""

    findings: list[Finding] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)


class Blocked(Exception):
    """하네스가 호출을 막았다."""

    def __init__(self, findings: list[Finding]):
        super().__init__(", ".join(f"{f.harness}:{f.kind}" for f in findings))
        self.findings = findings


class Harness(Protocol):
    name: str

    def before(self, req: LLMRequest, trace: Trace) -> LLMRequest: ...
    def after(self, resp: LLMResponse, trace: Trace) -> LLMResponse: ...


# --- 개인정보 가리기 ---

# 순서대로 적용한다. 앞에서 가린 자리표시([[RRN_1]] 등)는 뒤 규칙에 걸리지 않는다.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 주민등록번호: 생년월일 6자리 + 성별 자리(1~4) + 6자리
    ("RRN", re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?[1-4]\d{6}(?!\d)")),
    ("BIZNO", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")),  # 사업자등록번호
    (
        "PHONE",
        re.compile(
            r"(?<!\d)(?:0\d{1,2}[-.)\s]{1,2}\d{3,4}[-.\s]\d{4}|01[016789]\d{7,8}|1[5-9]\d{2}-\d{4})(?!\d)"
        ),
    ),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
]
_PLACEHOLDER = re.compile(r"\[\[(?:RRN|BIZNO|PHONE|EMAIL)_\d+\]\]")


def mask(text: str, table: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """(가린 글, {자리표시: 원래 값}). 같은 값은 같은 자리표시가 된다."""
    table = {} if table is None else table
    by_value = {v: k for k, v in table.items()}
    counts: dict[str, int] = {}
    for k in table:
        kind = k[2:].split("_")[0]
        counts[kind] = counts.get(kind, 0) + 1

    for kind, pattern in PII_PATTERNS:

        def sub(m: re.Match[str], kind: str = kind) -> str:
            value = m.group(0)
            if value not in by_value:
                counts[kind] = counts.get(kind, 0) + 1
                by_value[value] = f"[[{kind}_{counts[kind]}]]"
                table[by_value[value]] = value
            return by_value[value]

        text = pattern.sub(sub, text)
    return text, table


def unmask(value: Any, table: dict[str, str]) -> Any:
    """문자열·목록·사전 안의 자리표시를 원래 값으로 되돌린다."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: table.get(m.group(0), m.group(0)), value)
    if isinstance(value, list):
        return [unmask(v, table) for v in value]
    if isinstance(value, dict):
        return {k: unmask(v, table) for k, v in value.items()}
    return value


class PIIMask:
    name = "pii"

    def before(self, req: LLMRequest, trace: Trace) -> LLMRequest:
        masked, table = mask(req.input)
        trace.state[self.name] = table
        kinds: dict[str, int] = {}
        for placeholder in table:
            kind = placeholder[2:].split("_")[0]
            kinds[kind] = kinds.get(kind, 0) + 1
        trace.findings += [Finding(self.name, k, n) for k, n in sorted(kinds.items())]
        return replace(req, input=masked) if table else req

    def after(self, resp: LLMResponse, trace: Trace) -> LLMResponse:
        table = trace.state.get(self.name)
        return replace(resp, output=unmask(resp.output, table)) if table else resp


# --- 인젝션 탐지 ---

_AI = r"(?:AI|인공\s*지능|챗\s*봇|언어\s*모델|LLM|어시스턴트|assistant|GPT|Claude)"
# (규칙 이름, 강한 규칙인가, 패턴). 강한 규칙은 하나만 걸려도, 약한 규칙은 둘 이상 걸려야 의심한다.
INJECTION_RULES: list[tuple[str, bool, re.Pattern[str]]] = [
    (
        "override",  # 앞선 지시를 무시하라
        True,
        re.compile(
            r"(?:이전|앞선|위의?|기존|지금까지의?|모든|상기)\s*(?:의\s*)?(?:지시|명령|지침|프롬프트|규칙|설정)(?:\s*사항)?"
            r"\s*(?:들)?\s*(?:을|를|은|는)?\s*(?:모두\s*|전부\s*)?(?:무시|잊어|잊고|따르지\s*말)"
            r"|(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)"
            r"\s+(?:instructions?|prompts?|rules?|messages?|context)",
            re.IGNORECASE,
        ),
    ),
    (
        "role",  # 역할을 바꾸거나 시스템 프롬프트를 건드린다
        True,
        re.compile(
            r"시스템\s*프롬프트|system\s*prompt|developer\s*mode|개발자\s*모드|jailbreak|탈옥"
            r"|(?:너|당신)(?:는|은)\s*(?:이제|지금부터|더\s*이상)"
            r"|you\s+are\s+now\b|from\s+now\s+on,?\s+you|pretend\s+to\s+be|act\s+as\s+(?:an?\s+)?(?:ai|assistant|system)",
            re.IGNORECASE,
        ),
    ),
    (
        "hidden",  # 눈에 안 보이는 글자, 대화 형식 흉내
        True,
        re.compile(
            r"[\U000E0000-\U000E007F]|[‪-‮⁦-⁩]"
            r"|(?:[​-‍⁠﻿].{0,40}){4,}"
            r"|<\|(?:im_start|im_end|system|endoftext)\|>|\[/?INST\]"
            r"|^\s*(?:system|assistant)\s*:",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "addressed",  # 사람이 아니라 모델에게 말한다
        False,
        re.compile(
            rf"{_AI}\s*(?:에게|한테|야|여|아)(?=[\s,:.!])|{_AI}\s*(?:는|은)\s*(?:반드시|무조건|이\s*(?:문서|공고|내용))"
            rf"|(?:이\s*(?:문서|공고|글|내용)(?:을|를)\s*(?:읽|처리|분석|요약)(?:는|하는)\s*){_AI}"
            rf"|(?:dear|attention|note\s+to)\s+{_AI}",
            re.IGNORECASE,
        ),
    ),
    (
        "output",  # 출력을 지정한다
        False,
        re.compile(
            r"(?:라고|다고|으로|만)\s*(?:만\s*)?(?:출력|응답|답변|대답|판정|분류|기록)\s*(?:하라|해라|하세요|하시오|할\s*것|해야\s*한다|하십시오)"
            r"|(?:respond|reply|answer|output)\s+(?:only\s+)?with\b|(?:말하지|알리지|언급하지|출력하지)\s*(?:마|말)",
            re.IGNORECASE,
        ),
    ),
    (
        "exfil",  # 정보를 밖으로 보내라
        True,
        re.compile(
            r"(?:개인\s*정보|비밀\s*번호|API\s*키|토큰|프로필|대화\s*내용|지시\s*사항|프롬프트)(?:을|를)?\s*.{0,20}?"
            r"(?:https?://\S+\s*(?:로|으로)\s*)?(?:보내|전송|발송|출력|공개|알려)\s*(?:라|줘|주세요|주시오|하라|해라|하세요|할\s*것)"
            r"|(?:send|post|forward|reveal|leak)\s+.{0,40}?(?:to\s+https?://|your\s+(?:instructions|system\s*prompt))",
            re.IGNORECASE,
        ),
    ),
]
NEUTRALIZED = "(지시문으로 보여 가린 문장)"
_LINE_HEAD = re.compile(r"^(\[[^\]\n]{1,64}\]\s)?")  # 줄 머리의 [조각ID]는 남긴다


def scan(text: str) -> list[str]:
    """의심 규칙 이름들. 강한 규칙 하나, 또는 약한 규칙 둘 이상일 때만 돌려준다."""
    hits = [(name, strong) for name, strong, p in INJECTION_RULES if p.search(text)]
    if any(strong for _, strong in hits) or len(hits) >= 2:
        return [name for name, _ in hits]
    return []


class InjectionGuard:
    """입력을 줄 단위로 본다. policy: neutralize(그 줄만 가린다) / flag(기록만) / block(호출을 막는다)."""

    name = "injection"

    def __init__(self, policy: str = "neutralize"):
        if policy not in ("neutralize", "flag", "block"):
            raise ValueError(f"모르는 정책: {policy}")
        self.policy = policy

    def before(self, req: LLMRequest, trace: Trace) -> LLMRequest:
        lines, found = req.input.split("\n"), []
        for i, line in enumerate(lines):
            rules = scan(line)
            if not rules:
                continue
            head = _LINE_HEAD.match(line).group(0)
            found += [Finding(self.name, r, sample=line[len(head) :][:80]) for r in rules]
            if self.policy == "neutralize":
                lines[i] = head + NEUTRALIZED
        trace.findings += found
        if found and self.policy == "block":
            raise Blocked(found)
        return (
            replace(req, input="\n".join(lines)) if found and self.policy == "neutralize" else req
        )

    def after(self, resp: LLMResponse, trace: Trace) -> LLMResponse:
        return resp


def for_sensitivity(level: str) -> list[Harness]:
    """앱 설정의 민감도 등급에 맞는 기본 하네스. 만든 사람이 몰라도 기본값이 안전해야 한다.

    public    공개 문서만 다룬다: 인젝션 탐지
    personal  사용자가 준 정보가 섞인다: 개인정보 가리기 + 인젝션 탐지
    (sensitive 등급의 로컬 모델 라우팅은 아직 없다)
    """
    if level == "public":
        return [InjectionGuard()]
    if level == "personal":
        return [PIIMask(), InjectionGuard()]
    raise ValueError(f"모르는 민감도 등급: {level}")
