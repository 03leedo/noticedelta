"""설정 모듈: .env 읽기와 DB 열기."""

import sqlalchemy as sa

from noticedelta import config
from noticedelta.db import migrate


def test_load_env_keeps_existing_and_skips_empty(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# 주석\nA=1\nB=\nC = 3 \nNOEQUALS\n", encoding="utf-8")
    monkeypatch.setenv("A", "already")
    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("C", raising=False)
    config.load_env(env)
    import os

    assert os.environ["A"] == "already" and "B" not in os.environ and os.environ["C"] == "3"


def test_open_db_migrates_to_head(tmp_path):
    engine, factory = config.open_db(f"sqlite:///{tmp_path / 'c.db'}")
    assert migrate.current(engine) == migrate.head()
    with factory() as s:
        assert s.execute(sa.text("SELECT count(*) FROM program")).scalar() == 0
