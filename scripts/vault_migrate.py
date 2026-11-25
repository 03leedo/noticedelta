"""프로필 표의 값을 개인정보 금고로 옮긴다 (platform-services 4.7). 여러 번 돌려도 된다.

    uv run python scripts/vault_migrate.py          # 옮길 것만 센다
    uv run python scripts/vault_migrate.py --write  # 옮긴다: 금고에 암호화해 넣고, 동의를 남기고, 프로필의 값을 비운다

동의는 프로필에 남은 문구 판으로 판정·알림(필수)과 연구(선택 동의한 사람만)를 남긴다.
금고 열쇠는 NOTICEDELTA_VAULT_KEY, 없으면 data/vault.key(개발용).
DB를 최신 스키마(금고 표)로 올리므로, 금고 PR을 머지한 뒤에 돌린다.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
from noticedelta import vault  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.db.models import Profile  # noqa: E402
from noticedelta.grants import APP  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    load_env()
    _, factory = open_db()
    cipher = vault.load_cipher(ROOT / "data" / "vault.key")
    now = datetime.now(UTC)
    with factory() as s:
        rows = [p for p in s.scalars(sa.select(Profile)) if p.attributes]
        print(f"값이 남은 프로필 {len(rows)}개")
        if not args.write:
            return
        for p in rows:
            keys = list(p.attributes)
            vault.put(s, cipher, p.person_id, dict(p.attributes), source="input", now=now)
            purposes = ["judge", "notify", *(["research"] if p.research_ok else [])]
            for purpose in purposes:
                vault.grant_consent(
                    s, p.person_id, APP.id, purpose, keys, text_version=p.consent_version, now=now
                )
            p.attributes = {}
        s.commit()
        print(f"옮겼어요: {len(rows)}명")


if __name__ == "__main__":
    main()
