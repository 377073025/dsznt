"""数据库：SQLite（WAL + 外键），写法保持 PostgreSQL 兼容。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import DateTime, Integer, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """所有业务表带 created_at / updated_at / version（乐观锁）。"""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    if dbapi_conn.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = url or get_settings().db_url
    if url.startswith("sqlite:///") and ":memory:" not in url:
        from pathlib import Path

        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, future=True)
    if url.startswith("sqlite"):
        # pysqlite 默认的事务处理不支持 SAVEPOINT，按 SQLAlchemy 文档的做法改为显式 BEGIN
        @event.listens_for(_engine, "connect")
        def _no_autobegin(dbapi_conn, _record) -> None:
            dbapi_conn.isolation_level = None

        @event.listens_for(_engine, "begin")
        def _begin(conn) -> None:
            conn.exec_driver_sql("BEGIN")

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def create_all() -> None:
    import app.models  # noqa: F401  注册所有表

    Base.metadata.create_all(get_engine())


def get_engine() -> Engine:
    return _engine or init_engine()


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
