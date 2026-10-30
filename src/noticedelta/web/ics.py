"""내 공고의 마감일과 자격이 바뀌는 날을 캘린더(ICS)로 내보낸다 (RFC 5545)."""

from __future__ import annotations

from datetime import date, timedelta

from noticedelta.web.views import GROUP_NAMES, MyPrograms

RESULT_NAMES = {"eligible": "해당", "ineligible": "비해당", "unknown": "확인 필요"}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """한 줄은 75바이트를 넘지 않게 접는다. 이어지는 줄은 공백으로 시작한다."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return out


def _event(uid: str, day: date, summary: str, description: str, stamp: str) -> list[str]:
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}@noticedelta",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
        f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        "END:VEVENT",
    ]


def calendar(mine: MyPrograms) -> str:
    stamp = f"{mine.as_of:%Y%m%d}T000000Z"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//NoticeDelta//KO",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:내 지원사업 마감",
    ]
    for p in mine.items:
        if p.deadline is not None:
            lines += _event(
                f"deadline-{p.program_id}",
                p.deadline,
                f"[마감] {p.title}",
                f"판정: {GROUP_NAMES[p.group]}",
                stamp,
            )
        if p.next_change is not None:
            c = p.next_change
            lines += _event(
                f"change-{p.program_id}-{c.on:%Y%m%d}",
                c.on,
                f"[자격 변경] {p.title}",
                f"{RESULT_NAMES[c.before]} → {RESULT_NAMES[c.after]}",
                stamp,
            )
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"
