"""녹화본(cassette) 저장소. 로컬은 파일, 클라우드는 같은 인터페이스로 객체 스토리지."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class CassetteStore(Protocol):
    def load(self, key: str) -> dict | None: ...
    def save(self, key: str, record: dict) -> None: ...


class FileCassetteStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def load(self, key: str) -> dict | None:
        p = self._path(key)
        if not p.exists():
            return None
        record = json.loads(p.read_text(encoding="utf-8"))
        if record.get("key") != key:
            # 파일 이름과 안의 키가 다르면 바뀐 녹화본이다. 응답을 믿지 않고 없는 것으로 본다.
            return None
        return record

    def save(self, key: str, record: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
