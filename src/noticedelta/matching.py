"""이전 버전과 새 버전의 조각을 짝지어 lineage_id를 이어 준다 (설계 7.4)."""

from __future__ import annotations

import difflib
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from noticedelta.segments import Segment


class MatchMode(StrEnum):
    HASH = "hash"  # 해시가 같을 때만 같은 조각
    HASH_SIM = "hash_sim"  # 해시 + 순서 정렬 + 문자 n-gram 유사도


@dataclass
class ChangeSet:
    segments: list[Segment]  # 새 버전 조각, lineage_id 채워짐
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> set[str]:
        """바뀌거나 사라진 lineage (영향 필드 조회용)."""
        return set(self.modified) | set(self.removed)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def _ngrams(text: str, n: int = 3) -> set[str]:
    s = "".join(text.split())
    if len(s) < n:
        return set(s)
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def similarity(a: str, b: str) -> float:
    """문자 3-gram Jaccard 유사도."""
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga and not gb:
        return 1.0
    return len(ga & gb) / len(ga | gb)


def _default_id() -> str:
    return uuid.uuid4().hex[:12]


def _table_key(seg: Segment) -> tuple | None:
    if seg.kind != "table_cell":
        return None
    loc = seg.locator
    return (loc.get("table_sig"), loc.get("row_key"), loc.get("header"))


def match_segments(
    old: Sequence[Segment],
    new: Sequence[Segment],
    *,
    mode: MatchMode = MatchMode.HASH_SIM,
    threshold: float = 0.5,
    positional_floor: float = 0.1,
    new_id: Callable[[], str] = _default_id,
) -> ChangeSet:
    pair: dict[int, int] = {}  # new index -> old index
    modified_new: set[int] = set()

    # 1) 표 칸: (표 머리글 서명, 행 키, 열 머리글)로 먼저 짝짓는다.
    old_by_key: dict[tuple, deque[int]] = defaultdict(deque)
    for i, s in enumerate(old):
        if (k := _table_key(s)) is not None:
            old_by_key[k].append(i)
    for j, s in enumerate(new):
        k = _table_key(s)
        if k is not None and old_by_key.get(k):
            i = old_by_key[k].popleft()
            pair[j] = i
            if old[i].text_hash != s.text_hash:
                modified_new.add(j)

    # 2) 나머지는 해시 시퀀스 정렬로 순서가 같은 같은 조각을 잇는다.
    used_old = set(pair.values())
    rest_old = [i for i in range(len(old)) if i not in used_old]
    rest_new = [j for j in range(len(new)) if j not in pair]
    sm = difflib.SequenceMatcher(
        a=[old[i].text_hash for i in rest_old],
        b=[new[j].text_hash for j in rest_new],
        autojunk=False,
    )
    one_to_one: list[tuple[int, int]] = []  # 앞뒤가 그대로인 1:1 자리 교체
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pair[rest_new[j1 + k]] = rest_old[i1 + k]
        elif tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1:
            one_to_one.append((rest_old[i1], rest_new[j1]))

    # 3) 자리를 옮긴 같은 조각 (해시 일치, 순서대로).
    used_old = set(pair.values())
    left_old: dict[str, deque[int]] = defaultdict(deque)
    for i in range(len(old)):
        if i not in used_old:
            left_old[old[i].text_hash].append(i)
    for j in range(len(new)):
        if j not in pair and left_old.get(new[j].text_hash):
            pair[j] = left_old[new[j].text_hash].popleft()

    # 4) 유사도로 "수정된 조각" 짝짓기 (같은 종류끼리, 높은 순 탐욕).
    if mode is MatchMode.HASH_SIM:
        used_old = set(pair.values())
        cand_old = [i for i in range(len(old)) if i not in used_old]
        cand_new = [j for j in range(len(new)) if j not in pair]
        scored = sorted(
            (
                (similarity(old[i].text, new[j].text), -abs(i - j), i, j)
                for i in cand_old
                for j in cand_new
                if old[i].kind == new[j].kind
            ),
            reverse=True,
        )
        taken_old: set[int] = set()
        for score, _, i, j in scored:
            if score < threshold:
                break
            if i in taken_old or j in pair:
                continue
            pair[j] = i
            taken_old.add(i)
            modified_new.add(j)
        # 앞뒤 조각이 그대로인 1:1 자리 교체는 유사도가 낮아도 같은 조각으로 본다.
        for i, j in one_to_one:
            if (
                i not in taken_old
                and i not in used_old
                and j not in pair
                and old[i].kind == new[j].kind
                and similarity(old[i].text, new[j].text) >= positional_floor
            ):
                pair[j] = i
                modified_new.add(j)

    result = ChangeSet(segments=[])
    for j, s in enumerate(new):
        if j in pair:
            lid = old[pair[j]].lineage_id or new_id()
            (result.modified if j in modified_new else result.unchanged).append(lid)
        else:
            lid = new_id()
            result.added.append(lid)
        result.segments.append(replace(s, lineage_id=lid))
    paired_old = set(pair.values())
    result.removed = [
        old[i].lineage_id or new_id() for i in range(len(old)) if i not in paired_old
    ]
    return result
