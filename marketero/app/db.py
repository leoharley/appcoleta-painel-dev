from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Colunas acrescentadas depois da criação das tabelas (create_all não altera tabelas existentes).
_ADDED_COLUMNS = {
    "conversations": {
        "external_chat_id": "INTEGER",
        "external_assigned": "BOOLEAN NOT NULL DEFAULT 0",
        "needs_human": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "campaigns": {
        "template_ref": "VARCHAR(100) NOT NULL DEFAULT ''",
    },
}


def _migrate():
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")} if _is_sqlite else set()
            for col, ddl in cols.items():
                if _is_sqlite and col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        if _is_sqlite:
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_conversations_external_chat_id "
                                 "ON conversations (external_chat_id)")


def init_db():
    from . import models  # noqa: F401  (registra as tabelas)
    Base.metadata.create_all(engine)
    _migrate()
