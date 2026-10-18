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
      | (?:(?<=[~∼:])|(?<=[~∼:]\s)|(?<![\d.\w\s])|^)  # 연도 없는 "M. D."는 날짜 문맥에서만.
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


def _norm_list(v: Iterable[Any]) -> set[str]:
    return {normalize_text(str(x)) for x in v}


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
        return _norm_list(a) == _norm_list(b)
    return normalize_text(str(a)) == normalize_text(str(b))
