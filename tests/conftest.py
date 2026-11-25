import sqlalchemy as sa

from noticedelta.db import make_engine


def sqlite_engine() -> sa.Engine:
    """외래 키 검사와 SAVEPOINT가 제대로 도는 메모리 SQLite (noticedelta.db.make_engine)."""
    return make_engine("sqlite://")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    """금고 열쇠는 시험마다 새로 만든 것을 환경 변수로 준다(열쇠 파일을 만들지 않는다)."""
    from noticedelta.vault import KEY_ENV, Cipher

    monkeypatch.setenv(KEY_ENV, Cipher.new_key())
