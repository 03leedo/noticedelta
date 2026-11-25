"""개인정보 금고 (platform-services 4장): 동의 밖의 키를 읽는 길이 없다."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta import flight, vault
from noticedelta.db import make_engine, models

T0 = datetime(2026, 11, 25, 9, tzinfo=UTC)
APP = "grant-alerts"


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'v.db'}")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(engine)()
    cipher = vault.Cipher(vault.Cipher.new_key())
    vault.put(s, cipher, "p1", {"founded_on": "2021-03-02", "region_sido": "부산", "revenue_krw": 3_0000_0000},
              source="input", now=T0)  # fmt: skip
    vault.grant_consent(
        s, "p1", APP, "judge", ["founded_on", "region_sido"], text_version="v1", now=T0
    )
    yield s, cipher
    s.close()


def test_values_are_encrypted_at_rest(env):
    s, cipher = env
    raw = s.scalars(sa.select(models.VaultValueRow.ciphertext)).all()
    assert len(raw) == 3 and not any("부산" in c or "2021" in c for c in raw)
    with pytest.raises(vault.VaultError):  # 다른 열쇠로는 못 읽는다
        vault.Cipher(vault.Cipher.new_key()).decrypt(raw[0])


def test_no_path_reads_a_key_outside_consent(env):
    s, cipher = env
    # 동의한 키만 허가가 나고, 허가한 키만 꺼낸다
    g = vault.issue(s, "p1", APP, "judge", ["founded_on"], holder="svc:engine", now=T0)
    assert vault.read(s, cipher, g.id, now=T0) == {"founded_on": "2021-03-02"}
    with pytest.raises(vault.OutsideConsent):  # 매출은 동의하지 않았다
        vault.issue(s, "p1", APP, "judge", ["revenue_krw"], holder="svc:engine", now=T0)
    with pytest.raises(vault.OutsideConsent):  # 다른 용도·다른 앱은 동의가 없다
        vault.issue(s, "p1", APP, "assist", ["founded_on"], holder="session:1", now=T0)
    with pytest.raises(vault.OutsideConsent):
        vault.issue(s, "p1", "other-app", "judge", ["founded_on"], holder="svc:engine", now=T0)
    g2 = vault.issue(s, "p1", APP, "judge", ["founded_on"], holder="svc:engine", now=T0, uses=2)
    with pytest.raises(vault.GrantInvalid):  # 허가에 없는 키
        vault.read(s, cipher, g2.id, ["region_sido"], now=T0)
    # 여러 사람을 한꺼번에 꺼낼 때도 동의한 키만
    out = vault.for_purpose(
        s, cipher, APP, ["p1", "nobody"], purpose="judge", holder="svc:engine", now=T0
    )
    assert out == {"p1": {"founded_on": "2021-03-02", "region_sido": "부산"}}


def test_grants_expire_run_out_and_die_with_the_consent(env):
    s, cipher = env
    g = vault.issue(s, "p1", APP, "judge", ["founded_on"], holder="svc:engine", now=T0)
    vault.read(s, cipher, g.id, now=T0)
    with pytest.raises(vault.GrantInvalid):  # 한 번 쓰는 허가
        vault.read(s, cipher, g.id, now=T0)
    g = vault.issue(s, "p1", APP, "judge", ["founded_on"], holder="svc:engine", now=T0, uses=None)
    with pytest.raises(vault.GrantInvalid):  # 만료
        vault.read(s, cipher, g.id, now=T0 + timedelta(hours=1))
    g = vault.issue(s, "p1", APP, "judge", ["founded_on"], holder="svc:engine", now=T0, uses=None)
    vault.withdraw_consent(s, "p1", APP, "judge", now=T0)
    with pytest.raises(vault.GrantInvalid):  # 동의를 철회하면 허가도 바로 무효
        vault.read(s, cipher, g.id, now=T0)
    assert vault.for_purpose(s, cipher, APP, ["p1"], purpose="judge", holder="x", now=T0) == {}


def test_access_is_logged_without_values(env):
    s, cipher = env
    flight.open_trace(s, "T" * 26, kind="pipeline", started_at=T0, config={})
    with flight.bound(s, "T" * 26, lambda: T0):
        g = vault.issue(s, "p1", APP, "judge", ["region_sido"], holder="svc:engine", now=T0)
        vault.read(s, cipher, g.id, now=T0)
    log = s.scalars(sa.select(models.VaultAccessRow)).one()
    assert (log.purpose, log.keys, log.holder) == ("judge", ["region_sido"], "svc:engine")
    rec = s.scalars(sa.select(models.RecordRow)).one()
    assert rec.kind == "vault_access" and "부산" not in str(rec.payload)


def test_self_sees_everything_and_erase_keeps_only_the_withdrawal(env):
    s, cipher = env
    g = vault.issue(
        s, "p1", APP, "self", ["founded_on", "region_sido", "revenue_krw"], holder="link", now=T0
    )
    assert set(vault.read(s, cipher, g.id, now=T0)) == {"founded_on", "region_sido", "revenue_krw"}
    vault.erase(s, "p1", now=T0)
    assert s.scalar(sa.select(sa.func.count()).select_from(models.VaultValueRow)) == 0
    consent = s.scalars(sa.select(models.ConsentRow)).one()
    assert consent.withdrawn_at is not None  # 무엇에 언제 동의·철회했는지만 남는다


def test_judge_view_holds_only_consented_people_and_keys(env):
    s, cipher = env
    vault.put(s, cipher, "p2", {"region_sido": "서울"}, source="input", now=T0)  # 동의 없음
    view = vault.judge_view(s, cipher, APP, holder="svc:engine", now=T0)
    rows = s.execute(sa.select(view)).all()
    assert [(r.person_id, r.attributes) for r in rows] == [
        ("p1", {"founded_on": "2021-03-02", "region_sido": "부산"})
    ]
