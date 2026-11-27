"""보관 기한 정리: 기한 지난 값·오래된 허가·끝난 효과의 인자, 베타가 끝나면 모두 (retention.py)."""

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta import people, vault
from noticedelta.db import make_engine, models
from noticedelta.retention import purge
from noticedelta.tool_gateway import ToolGateway
from noticedelta.tooling import Tool

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(engine)()
    yield s
    s.close()


def person(s, cipher, at=T0):
    p, _ = people.create(s, at=at)
    s.add(models.Profile(person_id=p.id, attributes={}))
    vault.put(s, cipher, p.id, {"region_sido": "부산"}, source="input", now=at)
    vault.put(s, cipher, p.id, {"founded_on": "2021-01-01"}, source="input", now=at,
              expires_at=at + timedelta(days=10))  # fmt: skip
    vault.grant_consent(s, p.id, "grant-alerts", "judge", ["region_sido"], text_version="v", now=at)
    s.flush()
    return p.id


def test_expired_values_old_grants_and_finished_effect_arguments_go(db):
    cipher = vault.Cipher(vault.Cipher.new_key())
    pid = person(db, cipher)
    vault.issue(db, pid, "grant-alerts", "judge", ["region_sido"], holder="svc:engine", now=T0)
    sent = []
    tool = Tool("mail", "메일", lambda to: sent.append(to) or {"ok": True}, frozenset({"C"}),
                effect="external", hold=timedelta(0))  # fmt: skip
    gate = ToolGateway([tool], chosen=["mail"])
    sid = gate.open_session(db, app_id="a", agent="x", person_id=pid, now=T0).id
    out = gate.call(db, sid, "mail", {"to": "me@x.kr"}, now=T0)["action"]
    gate.commit_due(db, now=T0)
    later = T0 + timedelta(days=40)
    p = purge(db, now=later)
    db.commit()
    assert (p.vault_values, p.grants, p.actions) == (1, 1, 1)
    assert vault.keys_of(db, pid) == {"region_sido"}  # 기한 없는 값은 남는다
    a = db.get(models.ToolActionRow, out["id"])
    assert a.arguments == {} and a.status == "committed"  # 행과 상태는 남는다
    assert purge(db, now=later).actions == 0  # 두 번 지우지 않는다


def test_beta_end_removes_everyone_only_after_the_date(db):
    cipher = vault.Cipher(vault.Cipher.new_key())
    a, b = person(db, cipher), person(db, cipher)
    ends = date(2027, 3, 31)
    assert purge(db, now=datetime(2027, 3, 31, 12, tzinfo=UTC), beta_ends=ends,
                 end_beta=True).people == 0  # fmt: skip  # 그날까지는 지우지 않는다
    p = purge(db, now=datetime(2027, 4, 1, 1, tzinfo=UTC), beta_ends=ends, end_beta=True)
    db.commit()
    assert p.people == 2
    assert db.scalar(sa.select(sa.func.count()).select_from(models.Profile)) == 0
    assert vault.keys_of(db, a) == set() and vault.keys_of(db, b) == set()
    assert db.get(models.Person, a).deleted_at is not None  # 사람 ID는 가명으로 남는다
    assert purge(db, now=datetime(2027, 4, 2, tzinfo=UTC), beta_ends=ends).people == 0


def test_undo_info_survives_and_age_counts_from_the_end(db):
    board = []
    tick = Tool("tick", "체크", lambda step: board.append(step) or {"undo": {"step": step}},
                effect="internal", undo=lambda step: board.remove(step))  # fmt: skip
    gate = ToolGateway([tick], chosen=["tick"])
    sid = gate.open_session(db, app_id="a", agent="x", person_id=None, now=T0).id
    kept = gate.call(db, sid, "tick", {"step": "a"}, now=T0)["action"]["id"]
    late = gate.call(db, sid, "tick", {"step": "b"}, now=T0)["action"]["id"]
    gate.undo(db, late, by="person:p", now=T0 + timedelta(days=29))  # 요청은 오래전, 끝난 건 어제
    purge(db, now=T0 + timedelta(days=31))
    db.commit()
    a, b = db.get(models.ToolActionRow, kept), db.get(models.ToolActionRow, late)
    assert a.arguments == {} and a.undo == {"step": "a"}  # 반영된 효과는 여전히 되돌릴 수 있다
    assert b.arguments == {"step": "b"}  # 끝난 지 2일이라 아직 남긴다
    gate.undo(db, kept, by="person:p", now=T0 + timedelta(days=31))
    assert board == []


def test_beta_end_also_reaches_people_without_a_profile(db):
    cipher = vault.Cipher(vault.Cipher.new_key())
    pid = person(db, cipher)
    db.delete(db.get(models.Profile, pid))  # 프로필만 먼저 지워졌다
    db.flush()
    p = purge(db, now=datetime(2027, 4, 1, 1, tzinfo=UTC), beta_ends=date(2027, 3, 31),
              end_beta=True)  # fmt: skip
    db.commit()
    assert p.people == 1 and vault.keys_of(db, pid) == set()
    assert db.get(models.Person, pid).deleted_at is not None
    assert db.scalar(sa.select(sa.func.count()).select_from(models.AccessLink)) == 0
