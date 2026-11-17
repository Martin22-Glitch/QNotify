"""账号：一个账号一个库文件，账号名册放在主库里。

设计取舍：
- 主库（``settings.db_file``）同时是**主库归属账号**的库；第 2 个及以后的账号各开
  ``data/accounts/acc-<id>/qnotify.db``
- 通用资产（模型档案、系统设置、调用账本）只在主库；``global_db(db)`` 能把任何业务库换回主库
- 每个账号拿到的是**独立的 Database 实例**，m2 的 ``_state`` 缓存、建表都按实例生效

不变式（所有函数共同维护）：
1. 名册里永远至少有一个账号，``app_kv.base_account_id`` 永远指向名册里存在的那一行
2. 删除主库归属账号 = 同一事务里：清业务数据 → 删名册行 → 新建占位 → 改 base_account_id
3. 作用域只有一条规则：显式 ?account= → 协议端最后一次登录的号 → 主库归属账号
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError

from .db import Database

log = logging.getLogger(__name__)

ACCOUNTS_DDL = (
    "CREATE TABLE IF NOT EXISTS accounts ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " self_id INTEGER UNIQUE,"          # 未绑定（占位）时为 NULL
    " nickname TEXT NOT NULL DEFAULT '',"
    " role TEXT NOT NULL DEFAULT 'test',"  # test / main，只是标签
    " created_at TEXT NOT NULL,"
    " last_seen_at TEXT)",
    "CREATE TABLE IF NOT EXISTS app_kv ("
    " key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

BASE_KEY = "base_account_id"            # 主库归属的账号（不是"最小 id"）
LEGACY_CURRENT_KEY = "current_account_id"  # 已废弃的第二口径；启动时删除

#: 通用资产表：只在主库、删除任何账号都不碰。其余表一律视为账号资产（新表默认会被清）。
#: 注意：``settings`` 是 runtime_config / repo.get_settings_kv 用的那张表，若实际表名不同改这里。
# ── 表分类：显式登记。未登记 = 任何清理逻辑都不碰（并由 C5 用例与启动自检报出来） ──
SHARED_TABLES = frozenset({"accounts", "app_kv", "settings", "llm_profiles", "llm_usage", "maintenance_log"})

ACCOUNT_TABLES = frozenset({
    # 混合表：key LIKE PROFILE_SETTING_LIKE 的行是通用设置，清理时必须保留
    "profile",
    # 普通业务表
    "agent_memory", "agent_memory_turns", "chat_messages", "counters", "decisions", "feedback",
    "friends", "groups", "members", "memory_samples", "message_blocks", "messages",
    "chat_event_log", "chat_confirms", "focus_topics", "profile_history", "review_items", "rule_hits",
    "todo_items", "todo_sources", "user_rules",
    "whitelist_groups", "whitelist_members", "whitelist_users",
})
# 本库没有 FTS 虚表；将来加了就把虚表名写进这里，影子表自动带上
_FTS5_SHADOW = ("data", "idx", "content", "docsize", "config")


def _with_fts_shadows(*vtables: str) -> frozenset[str]:
    """FTS 虚表连同影子表一起登记（影子表也出现在 sqlite_master 里）。"""
    return frozenset(n for v in vtables for n in (v, *(f"{v}_{s}" for s in _FTS5_SHADOW)))


ACCOUNT_TABLES |= _with_fts_shadows()

if SHARED_TABLES & ACCOUNT_TABLES:
    raise RuntimeError(f"表同时被归为通用与账号资产：{sorted(SHARED_TABLES & ACCOUNT_TABLES)}")


class SharedAssetsTouched(RuntimeError):
    """按表清理的事务里通用资产行数变了；抛出即让 write() 事务回滚。"""


def table_class(name: str) -> str | None:
    if name in SHARED_TABLES:
        return "shared"
    if name in ACCOUNT_TABLES:
        return "account"
    return None


def _real_tables(db: Database) -> list[tuple[str, str]]:
    with db.session() as s:
        rows = s.execute(_sql("SELECT name, sql FROM sqlite_master WHERE type='table'")).all()
    return [(r.name, r.sql or "") for r in rows if not str(r.name).startswith("sqlite_")]


def unclassified_tables(db: Database) -> list[str]:
    return sorted(n for n, _ in _real_tables(db) if table_class(n) is None)




def shared_row_counts(s: Any) -> dict[str, int]:
    """在调用方事务内统计通用资产行数（含 profile 里的设置行）。"""
    conn = s.connection()
    present = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict[str, int] = {}
    for t in sorted(SHARED_TABLES & present):
        q = '"' + t.replace('"', '""') + '"'
        out[t] = int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {q}").scalar() or 0)
    if "profile" in present:
        out["profile#settings"] = int(
            conn.exec_driver_sql("SELECT COUNT(*) FROM profile WHERE key LIKE ?", (PROFILE_SETTING_LIKE,)).scalar() or 0
        )
    return out




#: ``profile`` 表混装"账号画像"和"全局设置"，后者键以此为前缀（与 m2.m2_settings 的读法一致）
PROFILE_SETTING_LIKE = "setting.%"

_SEEN_INTERVAL_SECONDS = 60.0   # 事件路径写 last_seen_at 的节流
_CLAIM_RETRIES = 3

_ROW_COLS = "id, self_id, nickname, role, created_at, last_seen_at"


class AccountNotFound(LookupError):
    """显式指定的账号不存在 / 不合法。main 里注册为 404。"""

    def __init__(self, raw: Any, message: str | None = None) -> None:
        self.raw = raw
        super().__init__(message or f"账号 {raw} 不存在")


class InvalidAccount(AccountNotFound):
    def __init__(self, raw: Any) -> None:
        super().__init__(raw, f"账号 {raw!r} 不合法")


#: 请求里显式给的账号原始值（?account= / X-QNotify-Account），由 ASGI 中间件搬运，不在中间件里校验
_REQUEST_ACCOUNT_RAW: ContextVar[str | None] = ContextVar("qn_request_account_raw", default=None)
#: 本次请求 / 本个事件 / 本轮后台判定已经算好的作用域账号 id（同一条规则的结果缓存，也供事件推送取 account_id）
_SCOPE_ACCOUNT: ContextVar[int | None] = ContextVar("qn_scope_account", default=None)

_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sql(q: str) -> Any:
    from sqlalchemy import text

    return text(q)


def _as_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# ------------------------------------------------------------------ 每个主库实例上的状态
def _lock(base: Database) -> threading.RLock:
    """名册变更与实例缓存共用一把可重入锁（touch → db_for 会嵌套）。"""
    lk = getattr(base, "_accounts_lock", None)
    if lk is None:
        with _GUARD:
            lk = getattr(base, "_accounts_lock", None)
            if lk is None:
                lk = threading.RLock()
                base._accounts_lock = lk  # type: ignore[attr-defined]
    return lk


def _cache(base: Database) -> dict[int, Database]:
    cache = getattr(base, "_account_dbs", None)
    if cache is None:
        cache = {}
        base._account_dbs = cache  # type: ignore[attr-defined]
    return cache


def _seen(base: Database) -> dict[int, float]:
    seen = getattr(base, "_accounts_seen", None)
    if seen is None:
        seen = {}
        base._accounts_seen = seen  # type: ignore[attr-defined]
    return seen


def global_db(db: Database) -> Database:
    """主库：模型档案、系统设置、调用账本这类"通用资产"只在主库，不跟账号走。"""
    return getattr(db, "_base_db", db)


# ------------------------------------------------------------------ 名册（会话内小工具）
def _read_base_id(s: Any) -> int | None:
    r = s.execute(_sql("SELECT value FROM app_kv WHERE key=:k"), {"k": BASE_KEY}).first()
    if r is None:
        return None
    try:
        return int(r.value)
    except (TypeError, ValueError):
        return None


def _write_base_id(s: Any, account_id: int) -> None:
    s.execute(
        _sql("INSERT INTO app_kv(key, value) VALUES(:k, :v) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
        {"k": BASE_KEY, "v": str(account_id)},
    )


def _row_exists(s: Any, account_id: int) -> bool:
    return s.execute(_sql("SELECT 1 FROM accounts WHERE id=:i"), {"i": account_id}).first() is not None


def _insert_placeholder(s: Any) -> int:
    return int(s.execute(
        _sql("INSERT INTO accounts(self_id, nickname, role, created_at) VALUES(NULL, '', 'test', :n)"),
        {"n": _now()},
    ).lastrowid)


# ------------------------------------------------------------------ 名册
def ensure_roster(db: Database) -> None:
    """主库里建名册表，并保证不变式 1。幂等；顺带修复旧版本留下的坏状态。"""
    base = global_db(db)
    with _lock(base), base.write() as s:
        conn = s.connection()
        for stmt in ACCOUNTS_DDL:
            conn.exec_driver_sql(stmt)
        s.execute(_sql("DELETE FROM app_kv WHERE key=:k"), {"k": LEGACY_CURRENT_KEY})

        bid = _read_base_id(s)
        if bid is not None and _row_exists(s, bid):
            return
        if bid is None:
            r = s.execute(_sql("SELECT MIN(id) AS i FROM accounts")).first()
            if r is not None and r.i is not None:
                _write_base_id(s, int(r.i))
                return
        else:
            log.warning("主库归属账号 id=%s 已不在名册（旧版本删除留下的），新建占位账号接管主库", bid)
        new_id = _insert_placeholder(s)
        _write_base_id(s, new_id)
        log.info("占位账号 id=%s 成为主库归属账号", new_id)


def list_all(db: Database) -> list[dict[str, Any]]:
    """名册。不含任何"当前账号"字段；``is_base`` 只表示"用主库文件"，``is_live`` 由路由补。"""
    base = global_db(db)
    with base.session() as s:
        rows = s.execute(_sql(f"SELECT {_ROW_COLS} FROM accounts ORDER BY id")).all()
        bid = _read_base_id(s)
    return [dict(r._mapping, is_base=(r.id == bid)) for r in rows]


def list_ids(db: Database) -> list[int]:
    base = global_db(db)
    with base.session() as s:
        return [int(r.id) for r in s.execute(_sql("SELECT id FROM accounts ORDER BY id")).all()]


def get(db: Database, account_id: int) -> dict[str, Any] | None:
    base = global_db(db)
    with base.session() as s:
        r = s.execute(_sql(f"SELECT {_ROW_COLS} FROM accounts WHERE id=:i"), {"i": account_id}).first()
    return dict(r._mapping) if r else None


def find_by_self_id(db: Database, self_id: int | None) -> dict[str, Any] | None:
    sid = _as_int(self_id)
    if sid is None:
        return None
    base = global_db(db)
    with base.session() as s:
        r = s.execute(_sql(f"SELECT {_ROW_COLS} FROM accounts WHERE self_id=:s"), {"s": sid}).first()
    return dict(r._mapping) if r else None


def base_account_id(db: Database) -> int | None:
    base = global_db(db)
    with base.session() as s:
        return _read_base_id(s)






def rename(db: Database, account_id: int, *, nickname: str | None = None,
           role: str | None = None) -> dict[str, Any] | None:
    base = global_db(db)
    sets, params = [], {"i": account_id}
    if nickname is not None:
        sets.append("nickname=:nk")
        params["nk"] = nickname
    if role is not None:
        sets.append("role=:r")
        params["r"] = role
    if sets:
        with base.write() as s:
            s.execute(_sql(f"UPDATE accounts SET {', '.join(sets)} WHERE id=:i"), params)
    return get(base, account_id)


# ------------------------------------------------------------------ 认领 / 新建
def _placeholder_ids(base: Database) -> list[int]:
    with base.session() as s:
        return [int(r.id) for r in s.execute(
            _sql("SELECT id FROM accounts WHERE self_id IS NULL ORDER BY id")).all()]


def _has_business_data(base: Database, account_id: int) -> bool:
    """占位账号的库里有没有消息。拿不准时按"有"处理（宁可新建，不可串号）。"""
    try:
        target = db_for(base, account_id)
    except AccountNotFound:
        return True
    try:
        with target.session() as s:
            return s.execute(_sql("SELECT 1 FROM messages LIMIT 1")).first() is not None
    except OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        return True


def _try_claim(base: Database, account_id: int, sid: int, nickname: str) -> bool:
    """原子条件更新：只有仍是占位时才认领。被别人抢先 / 唯一约束冲突都返回 False（调用方重查）。"""
    try:
        with base.write() as s:
            n = s.execute(
                _sql("UPDATE accounts SET self_id=:s, last_seen_at=:n, "
                     "nickname=CASE WHEN nickname='' THEN :nick ELSE nickname END "
                     "WHERE id=:i AND self_id IS NULL"),
                {"s": sid, "n": _now(), "nick": nickname, "i": account_id},
            ).rowcount
    except IntegrityError:
        return False
    return bool(n)


def _try_insert(base: Database, sid: int, nickname: str) -> int | None:
    try:
        with base.write() as s:
            return int(s.execute(
                _sql("INSERT INTO accounts(self_id, nickname, role, created_at, last_seen_at) "
                     "VALUES(:s, :nk, 'main', :n, :n)"),
                {"s": sid, "nk": nickname, "n": _now()},
            ).lastrowid)
    except IntegrityError:  # 同一 self_id 被并发建好了
        return None


def _mark_seen(base: Database, account_id: int) -> None:
    seen = _seen(base)
    now = time.monotonic()
    if now - seen.get(account_id, float("-inf")) < _SEEN_INTERVAL_SECONDS:
        return
    seen[account_id] = now
    with base.write() as s:
        s.execute(_sql("UPDATE accounts SET last_seen_at=:n WHERE id=:i"), {"n": _now(), "i": account_id})


def touch(db: Database, self_id: int | None, nickname: str = "", *, mark_seen: bool = True) -> dict[str, Any]:
    """按 self_id 找账号；没有就认领占位（仅当占位库没有消息）或新建。

    - ``self_id`` 为空：走作用域规则（显式 → 最后登录号 → 主库归属账号），不查任何"当前账号"指针
    - 并发安全：进程内一把锁 + 库级条件更新 / 唯一约束兜底；冲突时重查，不抛给调用方
    """
    base = global_db(db)
    sid = _as_int(self_id)
    if sid is None:
        return get(base, current_scope_id(base)) or {}

    with _lock(base):
        for _ in range(_CLAIM_RETRIES):
            row = find_by_self_id(base, sid)
            if row is not None:
                if mark_seen:
                    _mark_seen(base, int(row["id"]))
                return row

            claimed = False
            for pid in _placeholder_ids(base):
                if _has_business_data(base, pid):
                    continue
                if _try_claim(base, pid, sid, nickname):
                    _seen(base)[pid] = time.monotonic()
                    log.info("认领占位账号 id=%s self_id=%s", pid, sid)
                    return get(base, pid) or {}
                claimed = True  # 抢输了：回到循环顶部重查
                break
            if claimed:
                continue

            new_id = _try_insert(base, sid, nickname)
            if new_id is not None:
                _seen(base)[new_id] = time.monotonic()
                log.info("新账号 id=%s self_id=%s", new_id, sid)
                return get(base, new_id) or {}

    row = find_by_self_id(base, sid)
    if row is not None:
        return row
    raise RuntimeError(f"self_id={sid} 建账号失败（已重试 {_CLAIM_RETRIES} 次）")


# ------------------------------------------------------------------ 库实例
def _is_base_id(base: Database, account_id: int) -> bool:
    """是不是"主库归属账号"。db_path_of / db_for / db_for_scope 共用这一条判断。"""
    return account_id == base_account_id(base)


def _account_dir(base: Database, account_id: int) -> Path:
    return base.path.parent / "accounts" / f"acc-{account_id}"


def db_path_of(base: Database, account_id: int) -> Path:
    """主库归属账号用主库文件；其余各占一个目录。与 db_for / drop 同一条判断。"""
    base = global_db(base)
    if _is_base_id(base, account_id):
        return base.path
    return _account_dir(base, account_id) / "qnotify.db"


def _open_account_db(base: Database, account_id: int, path: Path) -> Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    inst = Database(path)
    inst._base_db = base  # type: ignore[attr-defined]
    inst._account_id = account_id  # type: ignore[attr-defined]
    inst.open()
    return inst


def db_for(base: Database, account_id: int | None) -> Database:
    """取某账号的 Database 实例（懒打开、加锁缓存）。不在名册里的 id → AccountNotFound，绝不凭空建库。"""
    base = global_db(base)
    if account_id is None:
        account_id = current_scope_id(base)
    if _is_base_id(base, account_id):
        return base
    with _lock(base):
        cache = _cache(base)
        got = cache.get(account_id)
        if got is not None:
            return got
        if get(base, account_id) is None:
            raise AccountNotFound(account_id)
        path = db_path_of(base, account_id)
        inst = _open_account_db(base, account_id, path)
        cache[account_id] = inst
        log.info("账号库就绪：id=%s %s", account_id, path)
        return inst




def dispose_all(base: Database) -> None:
    """进程退出时把账号库实例一并释放。"""
    base = global_db(base)
    with _lock(base):
        cache = _cache(base)
        items = list(cache.items())
        cache.clear()
    for acc_id, inst in items:
        try:
            inst.dispose()
        except Exception:
            log.warning("释放账号库失败 id=%s", acc_id, exc_info=True)


# ------------------------------------------------------------------ 删除
















# ------------------------------------------------------------------ 作用域（唯一口径）
def set_request_account_raw(raw: str | None) -> Token:
    return _REQUEST_ACCOUNT_RAW.set(raw)


def reset_request_account_raw(token: Token) -> None:
    _REQUEST_ACCOUNT_RAW.reset(token)


def bind_scope_account(account_id: int | None) -> Token:
    return _SCOPE_ACCOUNT.set(account_id)


def reset_scope_account(token: Token) -> None:
    _SCOPE_ACCOUNT.reset(token)


def scope_account() -> int | None:
    """本上下文已算好的作用域账号（给事件推送补 account_id 用）。"""
    return _SCOPE_ACCOUNT.get()


def explicit_account_id(db: Database) -> int | None:
    """请求显式给的账号；不存在 / 不合法 → AccountNotFound（404）。"""
    raw = _REQUEST_ACCOUNT_RAW.get()
    if raw is None or not str(raw).strip():
        return None
    try:
        acc_id = int(str(raw).strip())
    except ValueError as exc:
        raise InvalidAccount(raw) from exc
    if get(db, acc_id) is None:
        raise AccountNotFound(acc_id)
    return acc_id


def bind_live_source(db: Database, source: Callable[[], Any] | None) -> None:
    """登记"协议端最后一次登录的号"从哪儿取（main 绑到 NapCatClient.last_self_id）。"""
    global_db(db)._live_source = source  # type: ignore[attr-defined]


def bound_live_self_id(db: Database) -> int | None:
    src = getattr(global_db(db), "_live_source", None)
    if src is None:
        return None
    try:
        return _as_int(src())
    except Exception:
        return None


def live_self_id_of(client: Any) -> int | None:
    """协议端最后一次登录的号；断线 / 退避期间保持不变（只在客户端内存，不落库）。"""
    if client is None:
        return None
    v = getattr(client, "last_self_id", None)
    if v is None:
        v = getattr(client, "self_id", None)
    return _as_int(v)


def resolve_scope_id(db: Database, live_self_id: int | None = None) -> int:
    """作用域规则本体：显式 ?account= → 协议端最后一次登录的号 → 主库归属账号。

    登录号还不在名册里时**当场建**（认领规则同 touch），不会退回主库归属账号去显示别人的数据。
    """
    base = global_db(db)
    explicit = explicit_account_id(base)
    if explicit is not None:
        return explicit
    live = _as_int(live_self_id) if live_self_id is not None else bound_live_self_id(base)
    if live is not None:
        row = find_by_self_id(base, live) or touch(base, live, mark_seen=False)
        if row and row.get("id") is not None:
            return int(row["id"])
    bid = base_account_id(base)
    if bid is None or get(base, bid) is None:
        ensure_roster(base)
        bid = base_account_id(base)
    if bid is None:
        raise RuntimeError("名册不变式被破坏：没有主库归属账号")
    return bid


def current_scope_id(db: Database, live_self_id: int | None = None) -> int:
    """已绑定（本请求/本事件/本轮判定）就用绑定值，否则按规则现算。两者是同一条规则。"""
    bound = _SCOPE_ACCOUNT.get()
    if bound is not None:
        return bound
    return resolve_scope_id(db, live_self_id)


def db_for_scope(base: Database, live_self_id: int | None = None) -> Database:
    """取"当前作用域"的库。所有拿业务库的地方都走这里。"""
    return db_for(base, current_scope_id(base, live_self_id))


#: 兼容旧调用（同一条规则，不是第二口径）
db_for_request = db_for_scope
