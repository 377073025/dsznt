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


class RunBase(DeclarativeBase):
    """运行记录库（agent_run / agent_run_step）。

    单独放一个 SQLite 文件：运行记录要在作业的业务事务进行中实时写入、立即可见，且业务回滚时不能丢；
    SQLite 同一文件只有一个写者，和业务表放一起会互相锁住。PostgreSQL 下两者可以用同一个库。
    """


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
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_runs_engine: Engine | None = None
_RunsSession: sessionmaker[Session] | None = None


def runs_url_for(url: str) -> str:
    """运行记录库地址：SQLite 时与业务库同目录的 <名字>.runs.db；其他数据库与业务库相同。"""
    if url.startswith("sqlite:///") and ":memory:" not in url:
        path = url.removeprefix("sqlite:///")
        stem = path[:-3] if path.endswith(".db") else path
        return f"sqlite:///{stem}.runs.db"
    return url


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
    global _runs_engine, _RunsSession
    _runs_engine = create_engine(runs_url_for(url), future=True)
    _RunsSession = sessionmaker(bind=_runs_engine, expire_on_commit=False)
    return _engine


def create_all() -> None:
    import app.models  # noqa: F401  注册所有表

    Base.metadata.create_all(get_engine())
    RunBase.metadata.create_all(_runs_engine)
    _add_missing_columns(get_engine(), Base.metadata)
    _add_missing_columns(_runs_engine, RunBase.metadata)


def _literal(col) -> str | None:
    """新列的默认值写成 SQL 字面量；拿不到时返回 None（列允许为空）。"""
    d = col.default.arg if col.default is not None else None
    if callable(d):
        d = {list: [], dict: {}}.get(d)
        if d is None:
            return None
    if isinstance(d, bool):
        return "1" if d else "0"
    if isinstance(d, (int, float)):
        return str(d)
    if isinstance(d, str):
        return "'" + d.replace("'", "''") + "'"
    if isinstance(d, (list, dict)):
        import json

        return "'" + json.dumps(d) + "'"
    return None


def _add_missing_columns(engine: Engine, metadata) -> None:
    """SQLite 的轻量迁移：表已存在但缺少后来新增的列时补上（只加列，不改不删）。PostgreSQL 时改用正式迁移工具。"""
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import inspect

    with engine.begin() as conn:
        insp = inspect(conn)
        for table in metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                lit = _literal(col)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if lit is not None:
                    ddl += f" NOT NULL DEFAULT {lit}" if not col.nullable else f" DEFAULT {lit}"
                conn.exec_driver_sql(ddl)


def get_engine() -> Engine:
    return _engine or init_engine()


@contextmanager
def runs_scope() -> Iterator[Session]:
    """运行记录库的短事务。"""
    if _RunsSession is None:
        init_engine()
    assert _RunsSession is not None
    s = _RunsSession()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


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
