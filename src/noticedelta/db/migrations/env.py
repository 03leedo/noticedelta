"""Alembic 실행 환경. 모델(db/models.py)이 기준이고, SQLite는 batch 모드로 표를 바꾼다."""

from __future__ import annotations

import os

from alembic import context

from noticedelta.db import make_engine, models

config = context.config
target_metadata = models.Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or os.environ.get(
        "DATABASE_URL", "sqlite:///data/dev.db"
    )


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,  # SQLite는 ALTER가 약해 표를 새로 만들어 옮긴다
        **kwargs,
    )


def run_offline() -> None:
    _configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # migrate.upgrade가 연결을 넘겨준 경우
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url())
    with engine.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
