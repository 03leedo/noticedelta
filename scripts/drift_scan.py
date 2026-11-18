"""모은 수집 기록을 처음부터 훑어 형식 변화로 잡히는 키를 실행마다 보여 준다 (DB를 쓰지 않는다).

    uv run python scripts/drift_scan.py
    uv run python scripts/drift_scan.py --raw    # 출처 어댑터의 정리(주소 토큰 떼기 등) 없이 저장된 그대로

감시 규칙(drift.py)을 바꿀 때 지난 기록에서 무엇이 잡히는지 보는 회귀 시험이다. 직전 성공한 실행과
비교한다. 출력은 키 이름과 건수뿐이다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.config import blob_store, snapshots_root  # noqa: E402
from noticedelta.drift import detect  # noqa: E402
from noticedelta.programs import TO_RECORD  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="어댑터의 정리를 거치지 않은 원본으로 본다")
    args = ap.parse_args()
    store, root = blob_store(), snapshots_root()
    last: dict[str, dict[str, dict]] = {}
    for path in sorted((root / "runs").glob("*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        found = []
        for source, body in run["sources"].items():
            if body.get("error") is not None or source not in TO_RECORD:
                continue
            now = {}
            for it in body.get("items", []):
                raw = json.loads(store.get(it["item_key"]))
                now[it["external_id"]] = raw if args.raw else TO_RECORD[source](raw).raw
            if source in last:
                for key, s in detect(last[source], now).items():
                    found.append(f"{source}.{key} {s.kind} {s.changed}/{s.compared}")
            last[source] = now
        print(path.stem, "·", "; ".join(found) if found else "없음")


if __name__ == "__main__":
    main()
