import sqlalchemy as sa


def sqlite_engine() -> sa.Engine:
    """외래 키 검사를 켠 메모리 SQLite. SQLite는 기본으로 외래 키를 검사하지 않아
    PostgreSQL에서만 드러나는 INSERT 순서 오류를 놓친다."""
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine
