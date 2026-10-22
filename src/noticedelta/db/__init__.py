import sqlalchemy as sa

from noticedelta.db import models, repo


def make_engine(url: str, **kwargs) -> sa.Engine:
    """엔진을 만든다. SQLite면 외래 키 검사를 켜고 SAVEPOINT가 제대로 돌게 한다.

    pysqlite의 기본 트랜잭션 처리는 BEGIN을 늦게 내보내 SAVEPOINT(`begin_nested`)를 깨뜨린다.
    SQLAlchemy 문서의 방법대로 드라이버의 자동 트랜잭션을 끄고 BEGIN을 직접 낸다.
    """
    engine = sa.create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @sa.event.listens_for(engine, "connect")
        def _connect(dbapi_conn, _):
            dbapi_conn.isolation_level = None
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        @sa.event.listens_for(engine, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN")

    return engine


__all__ = ["make_engine", "models", "repo"]
