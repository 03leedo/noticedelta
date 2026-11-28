"""앱이 보는 조각 (app-spec 9장: 변경은 앱이 읽는 것 기준, 조각 단위).

    segs = first_view(app, "bizinfo", segments)                     # 첫 판: 앱이 읽는 조각만
    change, old, unknown = change_view(app, "bizinfo", change, old, unknown)   # 정정

- 본문 조각은 원본 키(locator.keys)가 앱이 읽는 키와 겹칠 때 본다. 키를 모르면(예전 판) 본다.
- 첨부 조각은 첨부를 읽는 앱만 본다. 읽지 못한 첨부의 lineage(unknown)도 마찬가지다.
- lineage는 옛 판과 새 판 중 어느 쪽에서든 보이면 남긴다. 옛 판의 근거가 새 판에서 다른 키로 넘어가도
  바뀐 것으로 잡히게(놓치는 것보다 한 번 더 보는 것이 낫다).
- 모두 보이면 받은 것을 그대로 돌려준다(앱 하나일 때와 같은 입력). 운영(pipeline)과 미리 돌려 보기
  (backtest)가 같은 함수를 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from noticedelta.appspec import AppDefinition, SourceUse
from noticedelta.matching import ChangeSet
from noticedelta.segments import Segment


def _use(app: AppDefinition, source: str | None) -> SourceUse | None:
    return next((u for u in app.sources if u.id == source), None)


def sees(use: SourceUse, locator: Mapping) -> bool:
    """이 위치의 조각을 앱이 보는가."""
    if locator.get("source") not in (None, "body"):
        return use.attachments
    keys = locator.get("keys")
    return keys is None or not set(use.read).isdisjoint(keys)


def first_view(app: AppDefinition, source: str | None, segments: list[Segment]) -> list[Segment]:
    use = _use(app, source)
    if use is None:
        return segments
    shown = [s for s in segments if sees(use, s.locator)]
    return segments if len(shown) == len(segments) else shown


def change_view(
    app: AppDefinition,
    source: str | None,
    change: ChangeSet,
    old: list[Segment],
    unknown: Iterable[str] = (),
) -> tuple[ChangeSet, list[Segment], set[str]]:
    unknown = set(unknown)
    use = _use(app, source)
    if use is None:
        return change, old, unknown
    if not use.attachments:
        unknown = set()  # 읽지 못한 첨부: 첨부를 읽지 않는 앱에게는 상관없다
    now = {s.lineage_id: sees(use, s.locator) for s in change.segments}
    before = {s.lineage_id: sees(use, s.locator) for s in old}
    if all(now.values()) and all(before.values()):
        return change, old, unknown

    def seen(lineage: str | None) -> bool:
        return now.get(lineage, False) or before.get(lineage, False)

    def keep(ids: list[str]) -> list[str]:
        return [i for i in ids if seen(i)]

    view = ChangeSet(
        [s for s in change.segments if seen(s.lineage_id)],
        keep(change.added),
        keep(change.removed),
        keep(change.modified),
        keep(change.unchanged),
    )
    return view, [s for s in old if seen(s.lineage_id)], unknown
