"""출처 형식 변화 감시 (source-catalog.md 5장).

한 번의 수집에서 항목 대부분의 같은 키가 한꺼번에 바뀌거나, 비거나, 새로 채워지면 정정이 아니라
출처가 형식을 바꾼 것으로 본다. 2026-10-30~11-17에 겪은 세 사건(첨부 주소 토큰, 따옴표 표기,
항목이 한꺼번에 빔)이 모두 이 모양이었다.

붙잡은 키는 마지막으로 본 값을 이어 둔다(변경으로 넘기지 않는다). 키마다 상태가 있다.

    held       붙잡고 운영자(사용자 출처는 소유자)의 결정을 기다린다. 작업 상태에 보인다
    ignored    운영자가 "이 키의 변화는 보지 않는다"고 정했다. 계속 이어 두되 보고하지 않는다
    released   운영자가 "진짜 변경이다"라고 정했다. 다음 적재 한 번은 값을 그대로 받고 closed가 된다
    recovered  값이 대부분 예전으로 돌아와 저절로 풀렸다
    closed     풀어 준 뒤 끝났다

여러 실행에 걸쳐 계속 붙잡히는 키는 호출마다 바뀌는 값일 수 있다(무시 후보).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from noticedelta.db.models import SourceDrift
from noticedelta.events import emit
from noticedelta.text import canonical_text

MIN_ITEMS = 10  # 한 실행에서 이만큼 넘는 항목이
SHARE = 0.5  # 비교한 항목의 이 비율 넘게 같은 키에서 바뀌면 형식 변화로 본다
RECOVER_SHARE = 0.1  # 붙잡은 키가 이 비율 아래로만 다르면 값이 돌아온 것으로 보고 푼다
SUSPECT_VOLATILE_RUNS = 3  # 이만큼 이어서 붙잡히면 호출마다 바뀌는 값일 수 있다
SAMPLES = 3

HELD, IGNORED, RELEASED, RECOVERED, CLOSED = "held", "ignored", "released", "recovered", "closed"
ACTIVE = (HELD, IGNORED, RELEASED)


def canonical(value: Any) -> Any:
    """비교용 값. 표기·공백 차이를 지우고, 빈 값은 None으로."""
    if value is None:
        return None
    if isinstance(value, str):
        return canonical_text(value) or None
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else None
    return value


@dataclass(frozen=True)
class Shift:
    key: str
    kind: str  # emptied(값이 비었다) / filled(새로 채워졌다) / changed(다른 값이 됐다)
    changed: int
    compared: int
    samples: tuple[str, ...] = ()  # 외부 ID 몇 개. 글은 남기지 않는다

    @property
    def share(self) -> float:
        return self.changed / self.compared if self.compared else 0.0


def shifts(
    before: Mapping[str, Mapping], now: Mapping[str, Mapping], *, skip: Iterable[str] = ()
) -> dict[str, Shift]:
    """키마다 바뀐 항목 수. before·now는 {외부 ID: 원본 항목}이고, 두 쪽에 다 있는 항목만 센다.

    비교한 수는 그 키가 한쪽에라도 값이 있는 항목 수다.
    """
    skip = set(skip)
    compared: Counter[str] = Counter()
    kinds: dict[str, Counter[str]] = {}
    samples: dict[str, list[str]] = {}
    for external_id, raw in now.items():
        old = before.get(external_id)
        if old is None:
            continue
        for key in (set(old) | set(raw)) - skip:
            a, b = canonical(old.get(key)), canonical(raw.get(key))
            if a is None and b is None:
                continue
            compared[key] += 1
            if a == b:
                continue
            kind = "emptied" if b is None else ("filled" if a is None else "changed")
            kinds.setdefault(key, Counter())[kind] += 1
            if len(samples.setdefault(key, [])) < SAMPLES:
                samples[key].append(external_id)
    return {
        key: Shift(key, c.most_common(1)[0][0], sum(c.values()), compared[key], tuple(samples[key]))
        for key, c in sorted(kinds.items())
    }


def detect(
    before: Mapping[str, Mapping],
    now: Mapping[str, Mapping],
    *,
    skip: Iterable[str] = (),
    min_items: int = MIN_ITEMS,
    share: float = SHARE,
) -> dict[str, Shift]:
    """형식 변화로 보이는 키: 한 실행에서 많은 항목의 같은 키가 한꺼번에 바뀌었다."""
    return {
        key: s
        for key, s in shifts(before, now, skip=skip).items()
        if s.changed >= min_items and s.share >= share
    }


def hold(raw: dict, old: Mapping | None, keys: Iterable[str]) -> list[str]:
    """붙잡은 키의 값을 마지막으로 본 값으로 되돌린다. 되돌린 키를 준다."""
    if old is None:
        return []  # 처음 보는 항목은 비교할 것이 없다
    out = []
    for key in keys:
        if canonical(raw.get(key)) == canonical(old.get(key)):
            continue
        if key in old:
            raw[key] = old[key]
        else:
            raw.pop(key, None)
        out.append(key)
    return out


def _now() -> datetime:
    return datetime.now(UTC)


class Watch:
    """출처 하나의 형식 변화 상태를 읽고, 한 실행을 보고 고친다. 적재(load)가 실행마다 부른다."""

    def __init__(self, session: Session, source: str):
        self.session, self.source = session, source
        rows = session.scalars(
            sa.select(SourceDrift).where(
                SourceDrift.source == source, SourceDrift.status.in_(ACTIVE)
            )
        ).all()
        self.rows = {r.key: r for r in rows}

    def keys(self, *statuses: str) -> set[str]:
        return {k for k, r in self.rows.items() if r.status in statuses}

    def observe(
        self, run: str, before: Mapping[str, Mapping], now: Mapping[str, Mapping]
    ) -> tuple[set[str], dict[str, dict]]:
        """(이번 실행에서 이어 둘 키, 실행 기록에 남길 내용)."""
        found = detect(before, now)
        for key, s in found.items():
            row = self.rows.get(key)
            if row is not None and row.status == RELEASED:
                continue  # 진짜 변경으로 받기로 했다. 이번에는 값을 그대로 받는다
            if row is None:
                row = SourceDrift(source=self.source, key=key, status=HELD, first_run=run, runs=0)
                self.session.add(row)
                self.rows[key] = row
                new = True
            else:
                new = False
            row.kind, row.changed, row.compared = s.kind, s.changed, s.compared
            row.samples, row.last_run, row.runs = list(s.samples), run, row.runs + 1
            if new:  # 처음 붙잡은 키. 필드를 다 채운 뒤에 낸다(봉투가 먼저 flush한다)
                emit(
                    self.session,
                    "source.drift_held",
                    {
                        "source": self.source,
                        "key": key,
                        "kind": s.kind,
                        "affected": s.changed,
                        "compared": s.compared,
                    },
                    producer="svc:loader",
                    key=f"{self.source}:{key}:{run}",
                )
        if self.keys(HELD) - set(found):
            # 붙잡았지만 이번에는 많은 항목에서 바뀌지 않았다. 값이 돌아왔으면 풀고, 아니면 계속 이어 둔다
            rest = shifts(before, now)
            for key in self.keys(HELD) - set(found):
                s = rest.get(key)
                if s is None or s.share <= RECOVER_SHARE:
                    self.rows[key].status = RECOVERED
                    self.rows[key].decided_at = _now()
        for key in self.keys(RELEASED):
            self.rows[key].status = CLOSED
        self.session.flush()
        record = {
            key: {"kind": s.kind, "changed": s.changed, "compared": s.compared}
            for key, s in found.items()
        }
        return self.keys(HELD, IGNORED), record


def decide(session: Session, source: str, key: str, status: str, note: str = "") -> SourceDrift:
    """운영자의 결정: ignored(이 키의 변화는 보지 않는다) 또는 released(진짜 변경으로 받는다)."""
    if status not in (IGNORED, RELEASED):
        raise ValueError(f"고를 수 있는 결정은 {IGNORED}, {RELEASED}뿐이다: {status}")
    row = session.scalar(
        sa.select(SourceDrift).where(
            SourceDrift.source == source, SourceDrift.key == key, SourceDrift.status.in_(ACTIVE)
        )
    )
    if row is None:
        raise LookupError(f"붙잡아 둔 키가 아니다: {source}.{key}")
    row.status, row.decided_at, row.note = status, _now(), note or row.note
    session.flush()
    emit(
        session,
        "source.drift_decided",
        {"source": source, "key": key, "status": status, "decided_by": "operator"},
        producer="svc:operator",
        key=f"{source}:{key}:{row.id}:{status}",
    )
    return row


def pending(session: Session) -> list[SourceDrift]:
    """결정을 기다리는 키."""
    return session.scalars(
        sa.select(SourceDrift).where(SourceDrift.status == HELD).order_by(SourceDrift.source)
    ).all()
