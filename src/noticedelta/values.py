"""한국어 공고의 날짜·금액을 정규값으로 바꾸고 타입별로 비교한다 (설계 7.7 ③)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from noticedelta.segments import normalize_text

KST = ZoneInfo("Asia/Seoul")
ValueType = Literal["datetime", "amount", "rules", "text", "list"]

_DATE = re.compile(
    r"""(?:
        (?P<y>\d{4}|'\d{2})\s*(?:[./-]|년)\s*(?P<m>\d{1,2})\s*(?:[./-]|월)\s*(?P<d>\d{1,2})(?:\s*일|\.)?
      | (?<!\d)(?P<cy>20\d{2})(?P<cm>[01]\d)(?P<cd>[0-3]\d)(?!\d)
      | (?<![\d.])(?P<m2>\d{1,2})\s*월\s*(?P<d2>\d{1,2})\s*일
      | (?<![\d.])                                      # 연도 없는 "M. D."는 날짜 문맥에서만.
        (?P<m3>\d{1,2})\.\s*(?P<d3>\d{1,2})\.(?!\d)         # "1. 2. 신청 자격" 같은 목록 번호와
        (?=\s*(?:\(|\d{1,2}\s*[:시]|오전|오후|까지|부터|[~∼]|$))  # 헷갈리지 않게 앞뒤를 본다
    )
    (?:\s*\([^)]{1,3}\))?
    (?:T(?P<iH>\d{2}):(?P<iMi>\d{2})(?::\d{2}(?:\.\d+)?)?(?P<tz>Z|[+-]\d{2}:?\d{2})?  # ISO 8601
      | \s*(?P<ap>오전|오후)?\s*(?P<H>\d{1,2})\s*(?::\s*(?P<Mi>\d{2})|시(?:\s*(?P<Ms>\d{1,2})\s*분)?)
    )?
    """,
    re.VERBOSE,
)

_UNITS = {
    "천억": 10**11,
    "백억": 10**10,
    "십억": 10**9,
    "조": 10**12,
    "억": 10**8,
    "천만": 10**7,
    "백만": 10**6,
    "십만": 10**5,
    "만": 10**4,
    "천": 10**3,
}
_UNIT_RE = "|".join(_UNITS)  # 긴 단위가 먼저 오도록 dict 순서 유지
_AMOUNT = re.compile(
    rf"(?:\d[\d,]*(?:\.\d+)?\s*(?:{_UNIT_RE})\s*)+(?:\d[\d,]*\s*원|원)?|\d[\d,]*(?:\.\d+)?\s*원"
)
_AMOUNT_TOKEN = re.compile(rf"(\d[\d,]*(?:\.\d+)?)\s*({_UNIT_RE})?")
_IGNORED_RULE_KEYS = {"id", "source", "confidence"}


def _to_datetime(m: re.Match, default_year: int | None) -> datetime | None:
    g = m.groupdict()
    if g["y"]:
        y = int(g["y"].lstrip("'"))
        year = 2000 + y if y < 100 else y
        month, day = int(g["m"]), int(g["d"])
    elif g["cy"]:
        year, month, day = int(g["cy"]), int(g["cm"]), int(g["cd"])
    else:
        if default_year is None:
            return None
        year = default_year
        month = int(g["m2"] or g["m3"])
        day = int(g["d2"] or g["d3"])
    hour, minute = 23, 59
    if g["iH"]:
        hour, minute = int(g["iH"]), int(g["iMi"])
        if hour == 24 and minute == 0:
            hour, minute = 23, 59
    elif g["H"]:
        hour = int(g["H"])
        minute = int(g["Mi"] or g["Ms"] or 0)
        if g["ap"] == "오후" and hour < 12:
            hour += 12
        elif g["ap"] == "오전" and hour == 12:
            hour = 0
        if hour == 24 and minute == 0:  # "24:00까지"는 그날 마감
            hour, minute = 23, 59
    tz = KST
    if g["tz"]:  # ISO 8601 시각대 (예: 2026-10-31T09:00:00Z)
        off = g["tz"].replace(":", "")
        sign = -1 if off.startswith("-") else 1
        tz = (
            UTC
            if off == "Z"
            else timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
        )
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(KST)
    except ValueError:
        return None


def find_datetimes(text: str, *, default_year: int | None = None) -> list[datetime]:
    """문장 안의 날짜(시각)를 모두 찾는다. 시각이 없으면 23:59(그날 마감)로 둔다."""
    found = []
    for m in _DATE.finditer(text):
        dt = _to_datetime(m, default_year)
        if dt is not None:
            found.append(dt)
    return found


def parse_kst_datetime(text: str, *, default_year: int | None = None) -> datetime | None:
    found = find_datetimes(text, default_year=default_year)
    return found[0] if found else None


def _amount_value(expr: str) -> int:
    total = 0.0
    for num, unit in _AMOUNT_TOKEN.findall(expr):
        total += float(num.replace(",", "")) * _UNITS.get(unit, 1)
    return round(total)


def find_amounts(text: str) -> list[int]:
    """원 단위 금액을 모두 찾는다. '원'이나 만·억·조 단위가 있어야 금액으로 본다."""
    out = []
    for m in _AMOUNT.finditer(text):
        expr = m.group(0)
        if "원" not in expr and not re.search(r"[만억조]", expr):
            continue
        out.append(_amount_value(expr))
    return out


def parse_krw(text: str) -> int | None:
    found = find_amounts(text)
    return found[0] if found else None


def _as_datetime(v: Any, default_year: int | None) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)
    return parse_kst_datetime(str(v), default_year=default_year)


def _as_amount(v: Any) -> int | None:
    if isinstance(v, int | float):
        return round(v)
    return parse_krw(str(v))


def _canonical(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _canonical(x) for k, x in sorted(v.items()) if k not in _IGNORED_RULE_KEYS}
    if isinstance(v, list | tuple | set):
        items = [_canonical(x) for x in v]
        return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
    if isinstance(v, str):
        return normalize_text(v)
    return v


_NOTE = re.compile(r"※.*$")
_LABEL = re.compile(r"^[^:：]{1,20}[:：]\s*")  # "등록특허/상표 : 특허등록원부" 의 앞말
_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]|【[^】]*】|（[^）]*）|<[^>]*>")
_QTY = re.compile(r"(각\s*)?\d+\s*(부|식|매|통|권|종)\.?\s*$")
_PUNCT = re.compile(
    r"[\s·•ㆍ‧∙・,.\-_'\"‘’“”/\ue000-\uf8ff]+"
)  # 사용자 정의 영역: HWP 기호 글꼴의 공백
_SPLIT = re.compile(r",|\s및\s|\s또는\s")
MIN_KEY = 3  # 이보다 짧은 열쇠는 버린다("등", "국세"처럼 나눠서 생긴 토막)


def list_item_key(item: Any) -> str:
    """목록 항목(서류 이름 등)의 비교용 열쇠: 수량, 서식 머리표, 괄호 설명, 띄어쓰기·문장부호를 뺀다.

    "[별지 1] 거래실적 총괄표 1부"와 "거래실적 총괄표", "장관표창에대한 동의서"와 "장관표창에 대한
    동의서"가 같은 열쇠가 된다(엔진 평가에서 서류 가짜 알림의 절반이 이런 표기 차이였다, 2026-11-25).
    """
    s = _PAREN.sub(" ", _NOTE.sub("", normalize_text(str(item))))
    s = _LABEL.sub("", s.strip())
    s = _QTY.sub("", s.strip())
    return _PUNCT.sub("", s)


def squash(text: str) -> str:
    """원문을 항목 열쇠와 맞대 볼 수 있게: 열쇠처럼 괄호 안과 띄어쓰기·문장부호를 뺀다(※ 뒤는 남긴다)."""
    return _PUNCT.sub("", _PAREN.sub(" ", normalize_text(text)))


def list_keys(items: Iterable[Any]) -> set[str]:
    """한 줄에 여러 서류를 적은 것("A, B 및 C")도 나눠서 본다. 양쪽을 같은 규칙으로 나누므로 공정하다."""
    keys = set()
    for x in items:
        whole = _PAREN.sub(
            " ", _NOTE.sub("", normalize_text(str(x)))
        )  # 괄호 안의 "또는"으로 나누지 않게
        for part in _SPLIT.split(whole):
            k = list_item_key(part)
            if len(k) >= MIN_KEY:
                keys.add(k)
    return keys


def _matched(key: str, others: set[str]) -> bool:
    """같거나, 한쪽이 다른 쪽에 들어 있으면 같은 항목("사업자등록증" ↔ "신청기업의사업자등록증명")."""
    return key in others or any(key in o or o in key for o in others)


def list_diff(a: Iterable[Any], b: Iterable[Any]) -> tuple[set[str], set[str]]:
    """(a에만 있는 열쇠, b에만 있는 열쇠)."""
    ka, kb = list_keys(a), list_keys(b)
    return {k for k in ka if not _matched(k, kb)}, {k for k in kb if not _matched(k, ka)}


def loads_lenient(raw: Any) -> Any:
    """LLM이 준 value_json을 읽는다. JSON이 아니면 글자 그대로 받는다(모양은 coerce_shape가 맞춘다).

    값을 JSON 문자열로 감싸라고 해도 맨 글자("2026-10-30 17:00")를 넣는 모델이 있다. 버리면
    있는 마감일을 놓친다(피클 학교 모델, 2026-11-25 추출 틀 비교에서 발견).
    """
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or None


def coerce_shape(value_type: ValueType, v: Any) -> Any | None:
    """LLM이 준 값을 타입에 맞는 모양으로. 맞출 수 없거나 비었으면 None(값 없음으로 본다).

    스키마는 value_json을 문자열로만 묶으므로 안의 모양은 모델마다 다르다. 예: 없는 필드를
    빼지 않고 null로 주는 모델이 있다(피클 학교 모델, 2026-11-24 평가에서 비교 단계가 깨졌다).
    """
    if v is None or v == "" or v == [] or v == {}:
        return None
    if value_type == "rules":
        return v if isinstance(v, dict) else None
    if value_type == "list":
        items = [v] if isinstance(v, str | int | float) else v
        if not isinstance(items, list):
            return None
        out = [str(x).strip() for x in items if isinstance(x, str | int | float) and str(x).strip()]
        return out or None
    if isinstance(v, bool) or not isinstance(v, str | int | float):
        return None
    if value_type == "text":
        return str(v).strip() or None
    return v


_TIME = re.compile(r"\d{1,2}:\d{2}")


def same_value(value_type: ValueType, a: Any, b: Any, *, default_year: int | None = None) -> bool:
    """values_equal과 같되, 날짜·시각은 한쪽에 시각이 없으면 날짜만 비교한다.

    출처 값(API 접수 기간)은 날짜만 주고 LLM 값은 시각까지 준다. 같은 날을 바뀐 것으로 보지 않게.
    """
    if value_type == "datetime" and not (_TIME.search(str(a)) and _TIME.search(str(b))):
        da, db = _as_datetime(a, default_year), _as_datetime(b, default_year)
        if da is not None and db is not None:
            return da.date() == db.date()
    return values_equal(value_type, a, b, default_year=default_year)


def values_equal(value_type: ValueType, a: Any, b: Any, *, default_year: int | None = None) -> bool:
    """두 값이 '진짜로' 같은지. 문구만 다르고 값이 같으면 True."""
    if value_type == "datetime":
        da, db = _as_datetime(a, default_year), _as_datetime(b, default_year)
        if da is None and db is None:  # "예산 소진 시까지" 같은 값은 문구로 비교
            return normalize_text(str(a)) == normalize_text(str(b))
        return da == db
    if value_type == "amount":
        aa, ab = _as_amount(a), _as_amount(b)
        if aa is None and ab is None:
            return normalize_text(str(a)) == normalize_text(str(b))
        return aa == ab
    if value_type == "rules":
        if hasattr(a, "model_dump"):
            a = a.model_dump()
        if hasattr(b, "model_dump"):
            b = b.model_dump()
        return _canonical(a) == _canonical(b)
    if value_type == "list":
        only_a, only_b = list_diff(a, b)
        return not only_a and not only_b
    return normalize_text(str(a)) == normalize_text(str(b))
