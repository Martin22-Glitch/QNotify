"""数据库：Engine/Session 生命周期、建表、版本迁移、写锁、计数器读写。

Engine 只在 Database.open() 里创建，open() 由 lifespan 调用，所以导入模块没有任何副作用。
SQLite 使用 WAL；所有写操作都通过 write_lock 串行。

每个库文件都带 PRAGMA user_version。open() 顺序：
  拒绝高于本程序的版本 → 建表（IF NOT EXISTS）→ 按需迁移并写 user_version。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from .models import SCHEMA_STATEMENTS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 版本与迁移

SCHEMA_VERSION = 1


class SchemaTooNew(RuntimeError):
    """库文件来自更新版本的程序：本程序不认识它的结构，拒绝打开（绝不降级改写）。"""

    def __init__(self, path: Path, found: int) -> None:
        super().__init__(f"{path} 的 schema 版本为 {found}，高于本程序支持的 {SCHEMA_VERSION}（库来自更新的版本）")
        self.path = path
        self.found = found


Migration = Callable[[sqlite3.Connection], None]


def _m001_baseline(conn: sqlite3.Connection) -> None:
    """v0 → v1：基线。表已由 create_schema 以 IF NOT EXISTS 建好，这里只把既有库认定为 v1。"""
    return None


# 规则：
# 1. 键 = 目标版本号，连续不跳号；已发布的步骤永不删改（老备份恢复时还要靠它们补齐）。
# 2. 每一步自身也要可重入（加列前先查 PRAGMA table_info、建表用 IF NOT EXISTS），双保险。
# 3. 只用 conn.execute()，禁止 executescript()（它会隐式 COMMIT，破坏"步骤+版本号"同事务）。
MIGRATIONS: dict[int, Migration] = {
    1: _m001_baseline,
}


def _raw_connect(path: Path) -> sqlite3.Connection:
    """原生连接到**已存在**的库文件（mode=rw：文件不存在时报错，绝不凭空建库）。"""
    uri = f"{Path(path).resolve().as_uri()}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _uv(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def read_user_version(path: Path) -> int:
    conn = _raw_connect(path)
    try:
        return _uv(conn)
    finally:
        conn.close()


def migrate_file(path: Path) -> tuple[int, int]:
    """把单个库文件迁移到 SCHEMA_VERSION，返回 (迁移前, 迁移后)。

    幂等：版本已是最新时什么都不做；否则在 BEGIN IMMEDIATE 里**重读**版本后执行缺的步骤，
    步骤与 user_version 同一事务提交（isolation_level=None + 显式 BEGIN，DDL 也真正事务化）。
    """
    conn = _raw_connect(path)
    try:
        start = _uv(conn)
        if start > SCHEMA_VERSION:
            raise SchemaTooNew(Path(path), start)
        if start == SCHEMA_VERSION:
            return start, start
        conn.execute("BEGIN IMMEDIATE")
        try:
            start = _uv(conn)  # 拿到写锁后重读：别的连接可能刚迁移完
            if start > SCHEMA_VERSION:
                raise SchemaTooNew(Path(path), start)
            for version in range(start + 1, SCHEMA_VERSION + 1):
                step = MIGRATIONS.get(version)
                if step is None:
                    raise RuntimeError(f"缺少迁移步骤 v{version}")
                step(conn)
                conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if start != SCHEMA_VERSION:
            log.info("库已迁移：%s v%s → v%s", path, start, SCHEMA_VERSION)
        return start, SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------- 已打开实例登记

# 只用于"启动阶段恢复"的防御：正常流程里恢复发生在任何 open() 之前，这里为空。
_OPEN_DATABASES: weakref.WeakSet[Database] = weakref.WeakSet()


def release_open_databases(under: Path) -> list[str]:
    """把本进程里路径位于 under 之下、仍处于打开状态的 Database 全部释放并返回其路径。

    正常启动时一个都没有。只有"进程还在跑却调用了启动恢复"（测试或误用）时才会命中：
    此时必须先断开旧连接，否则 Windows 上无法移动文件，旧连接也可能把 WAL 写到新库旁边。
    释放后这些实例不可再用（engine 属性会抛"数据库尚未打开"），进程应当重启。
    """
    root = Path(under).resolve()
    released: list[str] = []
    for inst in list(_OPEN_DATABASES):
        try:
            p = inst.path.resolve()
        except OSError:
            continue
        if p.parent == root or root in p.parents:
            log.warning("恢复前释放仍在使用的库连接：%s（本进程需重启）", p)
            inst.dispose()
            released.append(str(p))
    return released


# ---------------------------------------------------------------- 工具


def utc_now_iso() -> str:
    """ISO8601 UTC 文本，例如 2026-11-13T14:40:59Z。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _on_connect(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
    finally:
        cur.close()


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.write_lock = threading.Lock()
        self._engine: Engine | None = None
        self._factory: sessionmaker[Session] | None = None

    # ---------- 生命周期 ----------
    def open(self) -> None:
        if self._engine is not None:
            return
        in_memory = str(self.path) == ":memory:"
        if not in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                found = read_user_version(self.path)
                if found > SCHEMA_VERSION:
                    raise SchemaTooNew(self.path, found)
        engine = create_engine(
            URL.create("sqlite", database=str(self.path)),
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(engine, "connect", _on_connect)
        self._engine = engine
        self._factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            self.create_schema()
            if not in_memory:
                with self.write_lock:
                    migrate_file(self.path)
        except BaseException:
            self.dispose()
            raise
        _OPEN_DATABASES.add(self)
        log.info("数据库就绪：%s", self.path)

    def create_schema(self) -> None:
        with self.write_lock, self.engine.begin() as conn:
            for stmt in SCHEMA_STATEMENTS:
                conn.exec_driver_sql(stmt)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._factory = None
        _OPEN_DATABASES.discard(self)

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("数据库尚未打开")
        return self._engine

    @property
    def factory(self) -> sessionmaker[Session]:
        if self._factory is None:
            raise RuntimeError("数据库尚未打开")
        return self._factory

    # ---------- Session ----------
    @contextmanager
    def session(self) -> Iterator[Session]:
        """只读 Session（WAL 下读不需要加锁）。"""
        with self.factory() as s:
            yield s

    @contextmanager
    def write(self) -> Iterator[Session]:
        """串行写事务：持有写锁，正常退出提交，异常回滚并继续抛出。"""
        with self.write_lock:
            with self.factory() as s:
                try:
                    yield s
                    s.commit()
                except BaseException:
                    s.rollback()
                    raise

    # ---------- 计数器 ----------
    def incr_counter(self, name: str, delta: int = 1) -> None:
        with self.write() as s:
            s.execute(
                text(
                    "INSERT INTO counters (name, value) VALUES (:name, :delta) "
                    "ON CONFLICT(name) DO UPDATE SET value = counters.value + :delta"
                ),
                {"name": name, "delta": delta},
            )

    def get_counter(self, name: str) -> int:
        with self.session() as s:
            value = s.execute(
                text("SELECT value FROM counters WHERE name = :name"), {"name": name}
            ).scalar_one_or_none()
        return int(value or 0)

    # ---------- 自检 ----------
    def probe_writable(self) -> None:
        """真实执行一次写语句后回滚，不留下任何数据。失败直接抛异常。"""
        with self.write_lock, self.factory() as s:
            try:
                s.execute(
                    text(
                        "INSERT INTO counters (name, value) VALUES (:name, 0) "
                        "ON CONFLICT(name) DO UPDATE SET value = counters.value"
                    ),
                    {"name": "__selfcheck_probe__"},
                )
            finally:
                s.rollback()
