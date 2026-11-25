"""변경 이벤트마다 원본에서 바뀐 키를 보고, 앱이 읽는 변경인지 센다 (플랫폼 3번, app-spec 9장).

    uv run python scripts/change_keys.py           # 보고만
    uv run python scripts/change_keys.py --write   # 예전 이벤트에도 출처·바뀐 키·첨부 여부를 적어 둔다

출력은 숫자와 키 이름뿐이다(원문 없음).
"""

from __future__ import annotations

import argparse
from collections import Counter

import sqlalchemy as sa
from sqlalchemy.orm.attributes import flag_modified

from noticedelta.config import load_env, open_db
from noticedelta.db.models import ChangeEvent
from noticedelta.grants import APP
from noticedelta.history import event_keys, is_empty
from noticedelta.sources import CATALOG


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    rows: Counter[tuple[str, str, str]] = Counter()
    keys_only_unread: Counter[tuple[str, tuple[str, ...]]] = Counter()
    written = 0
    with factory() as s:
        events = s.scalars(
            sa.select(ChangeEvent).where(ChangeEvent.from_version_id.is_not(None))
        ).all()
        for ev in events:
            source, keys, attachments = event_keys(s, ev, CATALOG)
            reads = APP.reads_change(source, keys, attachments)
            shape = "조각 바뀜" if not is_empty(ev) else "조각 그대로"
            rows[(source, shape, "앱이 읽음" if reads else "앱이 안 읽음")] += 1
            if not reads:
                keys_only_unread[(source, tuple(keys or ()))] += 1
            if args.write and "keys" not in ev.changed_lineages:
                ev.changed_lineages = {
                    **ev.changed_lineages,
                    "source": source,
                    "keys": keys,
                    "attachments": attachments,
                }
                flag_modified(ev, "changed_lineages")
                written += 1
        if args.write:
            s.commit()

    print(f"앱: {APP.id} v{APP.version} · 첫 판을 뺀 변경 이벤트 {sum(rows.values())}건\n")
    print("| 출처 | 조각 | 앱 | 이벤트 |")
    print("|---|---|---|---|")
    for (source, shape, reads), n in sorted(rows.items()):
        print(f"| {source} | {shape} | {reads} | {n} |")
    if keys_only_unread:
        print("\n앱이 읽지 않는 키만 바뀐 이벤트:")
        for (source, keys), n in keys_only_unread.most_common():
            print(f"  {source} {', '.join(keys) or '(없음)'}: {n}")
    if args.write:
        print(f"\n예전 이벤트 {written}건에 바뀐 키를 적었다")


if __name__ == "__main__":
    main()
