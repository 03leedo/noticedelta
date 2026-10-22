import sqlalchemy as sa

from noticedelta.db import make_engine


def sqlite_engine() -> sa.Engine:
    """외래 키 검사와 SAVEPOINT가 제대로 도는 메모리 SQLite (noticedelta.db.make_engine)."""
    return make_engine("sqlite://")
