"""개인정보 금고 (platform-services 4장): 값은 암호화해 두고, 동의 안에서 범위를 줄인 허가로만 꺼낸다.

    put(session, cipher, person, {"founded_on": "2021-03-02"}, source="input", now=)
    grant_consent(session, person, app, "judge", keys, text_version=, now=)
    g = issue(session, person, app, "judge", keys, holder="svc:engine", now=)   # 동의 밖이면 OutsideConsent
    read(session, cipher, g.id, now=)                                          # 꺼낼 때마다 접근 기록

- 값은 사람 단위로 한 번 둔다. 어느 앱이 어느 키를 읽을 수 있는지는 동의(앱·용도·키)가 정한다.
- 허가는 (사람, 앱, 용도, 키 목록, 가진 쪽, 만료, 사용 횟수)다. 동의 범위를 넘는 허가는 만들 수 없고,
  동의를 철회하면 그 앱·용도의 허가는 바로 무효다.
- 꺼낼 때마다 접근 기록(vault_access)을 남긴다. 키 이름·용도·허가만, 값은 남기지 않는다. 비행 기록기의
  묶음이 걸려 있으면 거기에도 남긴다(record kind=vault_access).
- 본인(purpose=self)은 동의 없이 자기 것을 모두 본다. 본인의 링크로 들어온 화면이 쓴다.
- 암호화 열쇠는 환경 변수 NOTICEDELTA_VAULT_KEY(Fernet). 없으면 개발용으로 열쇠 파일을 만든다(data/,
  저장소 밖). DB 덤프나 백업이 새도 값은 읽히지 않는다. 서버 자체가 뚫리면 지키지 못한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from noticedelta import flight
from noticedelta.db.models import ConsentRow, VaultAccessRow, VaultGrantRow, VaultValueRow

KEY_ENV = "NOTICEDELTA_VAULT_KEY"
PURPOSES = ("judge", "notify", "assist", "research", "self")
SELF = "self"
GRANT_TTL = timedelta(minutes=10)


class VaultError(Exception):
    pass


class OutsideConsent(VaultError):
    """동의하지 않은 키나 용도로 허가를 달라고 했다."""


class GrantInvalid(VaultError):
    """없거나, 만료됐거나, 철회됐거나, 다 쓴 허가. 또는 허가에 없는 키를 꺼내려 했다."""


class Cipher:
    def __init__(self, key: bytes | str):
        self._f = Fernet(key)

    def encrypt(self, value: Any) -> str:
        return self._f.encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode(
            "ascii"
        )

    def decrypt(self, token: str) -> Any:
        try:
            return json.loads(self._f.decrypt(token.encode("ascii")))
        except InvalidToken as e:
            raise VaultError("금고 열쇠가 맞지 않는다") from e

    @staticmethod
    def new_key() -> str:
        return Fernet.generate_key().decode("ascii")


def load_cipher(dev_key_file: Path | None = None) -> Cipher:
    """환경 변수의 열쇠. 없으면 개발용 열쇠 파일(없으면 만든다). 운영에서는 환경 변수를 둔다."""
    if key := os.environ.get(KEY_ENV):
        return Cipher(key)
    if dev_key_file is None:
        raise VaultError(f"{KEY_ENV}가 없다")
    if not dev_key_file.exists():
        dev_key_file.parent.mkdir(parents=True, exist_ok=True)
        dev_key_file.write_text(Cipher.new_key(), encoding="ascii")
    return Cipher(dev_key_file.read_text(encoding="ascii").strip())


def _aware(t: datetime | None, now: datetime) -> datetime | None:
    if t is None or t.tzinfo:
        return t
    return t.replace(tzinfo=now.tzinfo)  # SQLite는 시간대를 지운다


# --- 값 ---


def put(
    session: Session,
    cipher: Cipher,
    person_id: str,
    values: Mapping[str, Any],
    *,
    source: str,
    now: datetime,
    expires_at: datetime | None = None,
) -> None:
    """값을 넣거나 바꾼다. None인 값은 지운다."""
    for key, value in values.items():
        row = session.get(VaultValueRow, (person_id, key))
        if value is None:
            if row is not None:
                session.delete(row)
            continue
        if row is None:
            row = VaultValueRow(person_id=person_id, key=key)
            session.add(row)
        row.ciphertext, row.source = cipher.encrypt(value), source
        row.updated_at, row.expires_at = now, expires_at
    session.flush()


def keys_of(session: Session, person_id: str) -> set[str]:
    return set(
        session.scalars(sa.select(VaultValueRow.key).where(VaultValueRow.person_id == person_id))
    )


# --- 동의 ---


def active_consent(
    session: Session, person_id: str, app_id: str, purpose: str
) -> ConsentRow | None:
    return session.scalar(
        sa.select(ConsentRow)
        .where(
            ConsentRow.person_id == person_id,
            ConsentRow.app_id == app_id,
            ConsentRow.purpose == purpose,
            ConsentRow.withdrawn_at.is_(None),
        )
        .order_by(ConsentRow.granted_at.desc())
        .limit(1)
    )


def grant_consent(
    session: Session,
    person_id: str,
    app_id: str,
    purpose: str,
    keys: Iterable[str],
    *,
    text_version: str | None,
    now: datetime,
) -> ConsentRow:
    """동의를 남긴다. 같은 앱·용도의 이전 동의는 철회로 닫고 새 행을 둔다(언제 무엇에 동의했는지 남게)."""
    if purpose not in PURPOSES or purpose == SELF:
        raise ValueError(f"동의할 수 없는 용도: {purpose}")
    old = active_consent(session, person_id, app_id, purpose)
    if old is not None:
        old.withdrawn_at = now
    row = ConsentRow(
        person_id=person_id,
        app_id=app_id,
        purpose=purpose,
        keys=sorted(set(keys)),
        text_version=text_version,
        granted_at=now,
    )
    session.add(row)
    session.flush()
    return row


def withdraw_consent(
    session: Session, person_id: str, app_id: str, purpose: str | None, *, now: datetime
) -> int:
    """동의를 철회한다(purpose가 None이면 그 앱의 모든 용도). 그 앱·용도의 허가는 바로 무효다."""
    q = sa.select(ConsentRow).where(
        ConsentRow.person_id == person_id,
        ConsentRow.app_id == app_id,
        ConsentRow.withdrawn_at.is_(None),
    )
    if purpose is not None:
        q = q.where(ConsentRow.purpose == purpose)
    rows = session.scalars(q).all()
    for row in rows:
        row.withdrawn_at = now
        _revoke(session, person_id, app_id, row.purpose, now)
    session.flush()
    return len(rows)


def _revoke(
    session: Session, person_id: str, app_id: str | None, purpose: str | None, now: datetime
) -> None:
    q = sa.update(VaultGrantRow).where(
        VaultGrantRow.person_id == person_id, VaultGrantRow.revoked_at.is_(None)
    )
    if app_id is not None:
        q = q.where(VaultGrantRow.app_id == app_id)
    if purpose is not None:
        q = q.where(VaultGrantRow.purpose == purpose)
    session.execute(q.values(revoked_at=now))


# --- 허가와 꺼내기 ---


def issue(
    session: Session,
    person_id: str,
    app_id: str,
    purpose: str,
    keys: Iterable[str],
    *,
    holder: str,
    now: datetime,
    ttl: timedelta = GRANT_TTL,
    uses: int | None = 1,
) -> VaultGrantRow:
    """허가를 만든다. 본인(self)이 아니면 그 앱·용도의 동의가 있어야 하고, 키는 동의한 키 안이어야 한다."""
    wanted = sorted(set(keys))
    if purpose != SELF:
        consent = active_consent(session, person_id, app_id, purpose)
        if consent is None:
            raise OutsideConsent(f"{app_id}의 {purpose} 동의가 없다")
        outside = set(wanted) - set(consent.keys)
        if outside:
            raise OutsideConsent(f"동의하지 않은 키: {sorted(outside)}")
    row = VaultGrantRow(
        person_id=person_id,
        app_id=app_id,
        purpose=purpose,
        keys=wanted,
        holder=holder,
        created_at=now,
        expires_at=now + ttl,
        uses_left=uses,
    )
    session.add(row)
    session.flush()
    return row


def read(
    session: Session,
    cipher: Cipher,
    grant_id: str,
    keys: Iterable[str] | None = None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """허가로 값을 꺼낸다(keys가 없으면 허가의 키 모두). 없는 값은 빠진다. 꺼낼 때마다 접근 기록."""
    g = session.get(VaultGrantRow, grant_id)
    if g is None or g.revoked_at is not None:
        raise GrantInvalid("없거나 철회된 허가")
    if _aware(g.expires_at, now) <= now:
        raise GrantInvalid("만료된 허가")
    if g.uses_left is not None and g.uses_left <= 0:
        raise GrantInvalid("다 쓴 허가")
    wanted = set(g.keys if keys is None else keys)
    if not wanted <= set(g.keys):
        raise GrantInvalid(f"허가에 없는 키: {sorted(wanted - set(g.keys))}")
    if g.purpose != SELF and active_consent(session, g.person_id, g.app_id, g.purpose) is None:
        raise GrantInvalid("동의가 철회됐다")
    rows = session.scalars(
        sa.select(VaultValueRow).where(
            VaultValueRow.person_id == g.person_id, VaultValueRow.key.in_(wanted)
        )
    ).all()
    out = {
        r.key: cipher.decrypt(r.ciphertext)
        for r in rows
        if r.expires_at is None or _aware(r.expires_at, now) > now
    }
    if g.uses_left is not None:
        g.uses_left -= 1
    session.add(
        VaultAccessRow(
            grant_id=g.id,
            person_id=g.person_id,
            app_id=g.app_id,
            purpose=g.purpose,
            keys=sorted(wanted),
            holder=g.holder,
            at=now,
        )
    )
    flight.record_here(
        "vault_access",
        ref=f"grant:{g.id}",
        payload={"purpose": g.purpose, "keys": sorted(wanted), "holder": g.holder},
    )
    session.flush()
    return out


def for_purpose(
    session: Session,
    cipher: Cipher,
    app_id: str,
    person_ids: Iterable[str],
    *,
    purpose: str,
    holder: str,
    now: datetime,
    keys: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """여러 사람의 값을 그 앱·용도의 동의 안에서 꺼낸다(판정, 알림). 동의가 없는 사람은 빠진다.

    keys를 주면 동의한 키와 겹치는 것만 꺼낸다(필요한 만큼만).
    """
    out: dict[str, dict[str, Any]] = {}
    for person_id in sorted(set(person_ids)):
        consent = active_consent(session, person_id, app_id, purpose)
        if consent is None:
            continue
        want = set(consent.keys) if keys is None else set(consent.keys) & set(keys)
        g = issue(session, person_id, app_id, purpose, want, holder=holder, now=now)
        out[person_id] = read(session, cipher, g.id, now=now)
    return out


def consented(session: Session, app_id: str, purpose: str) -> list[str]:
    """그 앱·용도에 지금 동의한 사람들."""
    return sorted(
        set(
            session.scalars(
                sa.select(ConsentRow.person_id).where(
                    ConsentRow.app_id == app_id,
                    ConsentRow.purpose == purpose,
                    ConsentRow.withdrawn_at.is_(None),
                )
            )
        )
    )


def judge_view(
    session: Session, cipher: Cipher, app_id: str, *, holder: str, now: datetime
) -> sa.Table:
    """대량 판정 SQL이 읽는 임시 표(person_id, attributes): 판정에 동의한 사람의 동의한 키만 풀어 둔다.

    세션의 연결에만 있는 임시 표라 커밋·연결이 끝나면 사라진다. 동의 밖의 키는 여기에 없다.
    """
    md = sa.MetaData()
    view = sa.Table(
        "vault_judge_view",
        md,
        sa.Column("person_id", sa.String(32), primary_key=True),
        sa.Column("attributes", sa.JSON),
        prefixes=["TEMPORARY"],
    )
    conn = session.connection()
    view.drop(conn, checkfirst=True)
    view.create(conn)
    people = for_purpose(
        session, cipher, app_id, consented(session, app_id, "judge"),
        purpose="judge", holder=holder, now=now,
    )  # fmt: skip
    if people:
        conn.execute(
            view.insert(), [{"person_id": p, "attributes": attrs} for p, attrs in people.items()]
        )
    return view


def erase(session: Session, person_id: str, *, now: datetime) -> None:
    """본인이 지운다: 값과 허가를 지우고 동의는 철회로 남긴다(무엇을 언제). 사람 ID는 남는다(가명)."""
    session.execute(sa.delete(VaultValueRow).where(VaultValueRow.person_id == person_id))
    session.execute(
        sa.update(ConsentRow)
        .where(ConsentRow.person_id == person_id, ConsentRow.withdrawn_at.is_(None))
        .values(withdrawn_at=now)
    )
    _revoke(session, person_id, None, None, now)
    session.flush()
