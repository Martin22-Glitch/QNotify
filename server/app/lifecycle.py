""" 生命周期：视图层"已过期"、保留期清理、/api/settings 的 retention.* 接入。

- 已过期只在视图里算，不改 todo_items.status
- 清理：先对话 → 再消息（及其判定、消息块）→ 最后待办；每批一个短写事务，批间让出
- "被引用则跳过"一律在 SQL 的 NOT EXISTS 里判，与删除同一事务（持写锁），不存在先查后删的竞态
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from typing import Any

from sqlalchemy import bindparam, text

from . import runtime_config
from .db import utc_now_iso
from .models import DONE_TODO_KEEP_DAYS, MAINT_CLEANUP, RETENTION_LAST_RUN_KEY, RETENTION_SETTING_KEYS

log = logging.getLogger(__name__)

BATCH_ROWS = 300                 # 每个写事务最多删这么多主表行（参考值 200–500）
BATCH_PAUSE_SECONDS = 0.05       # 批间让出，给实时入库/判定拿写锁
MAX_BATCHES_PER_PHASE = 5000     # 防御性上限，避免任何意外死循环
FIRST_RUN_DELAY_SECONDS = 600.0  # 启动 10 分钟后才考虑第一轮，不和启动补采抢
RUN_INTERVAL_SECONDS = 86400.0
RETRY_SECONDS = 3600.0

# ================================================================ 时间与"已过期"


def parse_ts(value: Any, tz: tzinfo, *, date_end: bool = False) -> datetime | None:
    """宽松解析为带时区的 datetime。纯日期：date_end=True 取当天 23:59:59.999999（本地时区）。
    无时区的字符串按本地时区；解析不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        if v > 1e12:  # 毫秒
            v /= 1000.0
        return datetime.fromtimestamp(v, UTC)
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 10:
        try:
            d = date.fromisoformat(s)
        except ValueError:
            return None
        return datetime.combine(d, dtime.max if date_end else dtime.min, tzinfo=tz)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def policy_now(tz: tzinfo) -> datetime:
    """视图口径的"现在"。与 m2._today 一致：QN_FAKE_TODAY 冻结日期（保留当前钟点），检查可复现。"""
    real = datetime.now(tz)
    fake = os.environ.get("QN_FAKE_TODAY", "").strip()
    if not fake:
        return real
    try:
        d = date.fromisoformat(fake[:10])
    except ValueError:
        return real
    return datetime.combine(d, real.timetz())


@dataclass(frozen=True, slots=True)
class ExpiryPolicy:
    now: datetime
    overdue_days: int
    tz: tzinfo

    def expired_since(self, t: dict) -> datetime | None:
        """已过期则返回"从何时起算过期"，否则 None。
        open 且 due_at + N 天 < now；或 open、无截止日、created_at + 2N 天 < now（都是严格小于）。"""
        if t.get("status") != "open":
            return None
        n = timedelta(days=self.overdue_days)
        due = parse_ts(t.get("due_at"), self.tz, date_end=True)
        if due is not None:
            since = due + n
        else:
            created = parse_ts(t.get("created_at"), self.tz)
            if created is None:
                return None  # 时间全坏的留在主视图，让人看得见、能处理
            since = created + 2 * n
        return since if since < self.now else None

    def is_expired(self, t: dict) -> bool:
        return self.expired_since(t) is not None

    def main_sort_key(self, t: dict) -> tuple:
        """主视图：有截止日的按截止时间升序（逾期最久在最上 → 今天 → 之后），无截止日的垫底、新的在前。"""
        due = parse_ts(t.get("due_at"), self.tz, date_end=True)
        if due is not None:
            return (0, due.timestamp(), int(t["id"]))
        return (1, 0.0, -int(t["id"]))

    def history_sort_key(self, t: dict) -> tuple:
        """历史：按 updated_at 倒序（调用方 reverse=True）。"""
        upd = parse_ts(t.get("updated_at"), self.tz)
        return (upd.timestamp() if upd else 0.0, int(t["id"]))


def make_policy(tz: tzinfo, overdue_days: int) -> ExpiryPolicy:
    return ExpiryPolicy(now=policy_now(tz), overdue_days=int(overdue_days), tz=tz)


def retention_view(cfg: runtime_config.RetentionConfig) -> dict[str, int]:
    # 前后端统一用短键（GET 与 PUT 一致）；库里仍存 "retention." 前缀
    return {"overdue_days": cfg.overdue_days, "content_days": cfg.content_days}


def expired_summary(open_rows: list[dict], policy: ExpiryPolicy, acked_raw: str | None) -> dict:
    sinces = [s for s in (policy.expired_since(t) for t in open_rows) if s is not None]
    newest = max(sinces) if sinces else None
    acked = parse_ts(acked_raw, UTC) if acked_raw else None
    show = bool(sinces) and newest is not None and (acked is None or acked < newest)
    return {
        "count": len(sinces),
        "newest_at": iso_utc(newest) if newest else None,
        "acked_at": acked_raw or None,
        "show": show,
    }


def ack_value(summary: dict) -> str:
    """已读时间取 max(现在, newest_at)，保证 acked_at 不早于当前最新一件（QN_FAKE_TODAY 在未来时也成立）。"""
    now = datetime.now(UTC)
    newest = parse_ts(summary.get("newest_at"), UTC) if summary.get("newest_at") else None
    return iso_utc(newest if newest is not None and newest > now else now)


# ================================================================ 清理：计划（按库内省）


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema(s: Any) -> dict[str, set[str]]:
    """表名 → 列名集合。跳过 sqlite 内部表、虚表（FTS）及其影子表：它们不是"引用"。"""
    rows = s.execute(text("SELECT name, sql FROM sqlite_master WHERE type='table'")).all()
    virtual = [str(r[0]) for r in rows if str(r[1] or "").lstrip().upper().startswith("CREATE VIRTUAL")]
    out: dict[str, set[str]] = {}
    for r in rows:
        name = str(r[0])
        if name.startswith("sqlite_") or name in virtual or any(name.startswith(v + "_") for v in virtual):
            continue
        out[name] = {str(c[1]) for c in s.execute(text(f"PRAGMA table_info({_q(name)})")).all()}
    return out


_MSG_OWNED = frozenset({"messages", "decisions", "message_blocks"})  # 随消息一起删的，不算"引用"
_BLOCK_TIME_COLS = ("end_time", "last_time", "last_msg_time", "closed_at", "time",
                    "start_time", "created_at", "updated_at")


@dataclass
class _Plan:
    chat: bool = False
    msgs: bool = False
    dec_by_msg: bool = False
    dec_by_block: bool = False
    block_time_col: str | None = None
    todos: bool = False
    todo_sources: bool = False
    msg_guards: list[str] = field(default_factory=list)
    block_guards: list[str] = field(default_factory=list)
    todo_guards: list[str] = field(default_factory=list)
    protectors: list[str] = field(default_factory=list)


def _build_plan(db: Any) -> _Plan:
    p = _Plan()
    with db.write() as s:
        cols = _schema(s)
        # 引用判断要走索引（只加索引，不加表）
        for t, c in (("todo_sources", "msg_id"), ("todo_sources", "block_id"), ("feedback", "msg_id")):
            if c in cols.get(t, set()):
                s.execute(text(f"CREATE INDEX IF NOT EXISTS {_q(f'idx_qn_{t}_{c}')} ON {_q(t)} ({c})"))

        dec = cols.get("decisions", set())
        p.chat = "chat_messages" in cols and "created_at" in cols["chat_messages"]
        p.msgs = {"id", "msg_id", "time"} <= cols.get("messages", set())
        p.dec_by_msg = "msg_id" in dec
        p.dec_by_block = "block_id" in dec

        # ---- 消息：任何其它表（todo_sources / memory_samples / feedback / 复核队列……）带 msg_id 指向它 → 保留
        for t, c in sorted(cols.items()):
            if t in _MSG_OWNED:
                continue
            if "msg_id" in c:
                p.msg_guards.append(f"NOT EXISTS (SELECT 1 FROM {_q(t)} r WHERE r.msg_id = m.msg_id)")
                p.protectors.append(f"{t}.msg_id")
        # 判定被别的表引用（如复核项 decision_id）→ 判定和它的消息都保留
        if p.dec_by_msg and "id" in dec:
            for t, c in sorted(cols.items()):
                if t != "decisions" and "decision_id" in c:
                    p.msg_guards.append(
                        f"NOT EXISTS (SELECT 1 FROM decisions d JOIN {_q(t)} r ON r.decision_id = d.id "
                        f"WHERE d.msg_id = m.msg_id)")
                    p.protectors.append(f"{t}.decision_id")

        # ---- 消息块
        blk = cols.get("message_blocks", set())
        if "id" in blk:
            p.block_time_col = next((c for c in _BLOCK_TIME_COLS if c in blk), None)
            for t, c in sorted(cols.items()):
                if t in ("message_blocks", "decisions"):
                    continue
                if "block_id" in c:
                    p.block_guards.append(f"NOT EXISTS (SELECT 1 FROM {_q(t)} r WHERE r.block_id = b.id)")
            if p.dec_by_block and p.dec_by_msg:  # 块里还有活着的消息 → 块留着
                p.block_guards.append(
                    "NOT EXISTS (SELECT 1 FROM decisions d JOIN messages m ON m.msg_id = d.msg_id "
                    "WHERE d.block_id = b.id)")
            if p.dec_by_block and "id" in dec:
                for t, c in sorted(cols.items()):
                    if t != "decisions" and "decision_id" in c:
                        p.block_guards.append(
                            f"NOT EXISTS (SELECT 1 FROM decisions d JOIN {_q(t)} r ON r.decision_id = d.id "
                            f"WHERE d.block_id = b.id)")

        # ---- 待办：todo_sources 随待办删；feedback 是学习日志，不算"引用"；其它带 todo_id 的表 → 保留
        p.todos = {"id", "status", "updated_at"} <= cols.get("todo_items", set())
        p.todo_sources = "todo_id" in cols.get("todo_sources", set())
        for t, c in sorted(cols.items()):
            if t in ("todo_items", "todo_sources", "feedback"):
                continue
            if "todo_id" in c:
                p.todo_guards.append(f"NOT EXISTS (SELECT 1 FROM {_q(t)} r WHERE r.todo_id = ti.id)")
    return p


def _ids_param(sql: str, *names: str) -> Any:
    return text(sql).bindparams(*(bindparam(n, expanding=True) for n in names))


# ================================================================ 清理：单批（线程里跑，每批一个短写事务）


def _chat_batch(db: Any, cut_iso: str) -> tuple[int]:
    with db.write() as s:
        r = s.execute(
            text("DELETE FROM chat_messages WHERE id IN (SELECT id FROM chat_messages "
                 "WHERE julianday(created_at) < julianday(:c) ORDER BY id LIMIT :n)"),
            {"c": cut_iso, "n": BATCH_ROWS},
        )
        return (int(r.rowcount or 0),)


def _msg_batch(db: Any, plan: _Plan, cut_epoch: int) -> tuple[int, int]:
    where = " AND ".join(["m.time < :cut", *plan.msg_guards])
    with db.write() as s:
        rows = s.execute(
            text(f"SELECT m.id, m.msg_id FROM messages m WHERE {where} ORDER BY m.time, m.id LIMIT :n"),
            {"cut": cut_epoch, "n": BATCH_ROWS},
        ).all()
        if not rows:
            return (0, 0)
        ids = [int(r[0]) for r in rows]
        mids = sorted({int(r[1]) for r in rows})
        dec = 0
        if plan.dec_by_msg:  # 同 msg_id 还有别的消息留着（群/私聊撞号）就不删它的判定
            dec = int(s.execute(
                _ids_param("DELETE FROM decisions WHERE msg_id IN :mids AND NOT EXISTS ("
                           "SELECT 1 FROM messages o WHERE o.msg_id = decisions.msg_id AND o.id NOT IN :ids)",
                           "mids", "ids"),
                {"mids": mids, "ids": ids},
            ).rowcount or 0)
        n = int(s.execute(_ids_param("DELETE FROM messages WHERE id IN :ids", "ids"), {"ids": ids}).rowcount or 0)
        return (n, dec)


def _block_batch(db: Any, plan: _Plan, cut_epoch: int, cut_iso: str) -> tuple[int, int]:
    c = f"b.{_q(plan.block_time_col or '')}"
    old = (f"((typeof({c}) IN ('integer','real') AND {c} < :cut) "
           f"OR (typeof({c}) = 'text' AND julianday({c}) < julianday(:cut_iso)))")
    where = " AND ".join([old, *plan.block_guards])
    with db.write() as s:
        ids = [int(r[0]) for r in s.execute(
            text(f"SELECT b.id FROM message_blocks b WHERE {where} ORDER BY b.id LIMIT :n"),
            {"cut": cut_epoch, "cut_iso": cut_iso, "n": BATCH_ROWS},
        ).all()]
        if not ids:
            return (0, 0)
        dec = 0
        if plan.dec_by_block:
            dec = int(s.execute(_ids_param("DELETE FROM decisions WHERE block_id IN :ids", "ids"),
                                {"ids": ids}).rowcount or 0)
        n = int(s.execute(_ids_param("DELETE FROM message_blocks WHERE id IN :ids", "ids"),
                          {"ids": ids}).rowcount or 0)
        return (n, dec)


def _todo_batch(db: Any, plan: _Plan, cut_iso: str) -> tuple[int]:
    where = " AND ".join(["ti.status IN ('done','dropped')",
                          "julianday(ti.updated_at) < julianday(:c)", *plan.todo_guards])
    with db.write() as s:
        ids = [int(r[0]) for r in s.execute(
            text(f"SELECT ti.id FROM todo_items ti WHERE {where} ORDER BY ti.id LIMIT :n"),
            {"c": cut_iso, "n": BATCH_ROWS},
        ).all()]
        if not ids:
            return (0,)
        if plan.todo_sources:
            s.execute(_ids_param("DELETE FROM todo_sources WHERE todo_id IN :ids", "ids"), {"ids": ids})
        n = int(s.execute(_ids_param("DELETE FROM todo_items WHERE id IN :ids", "ids"), {"ids": ids}).rowcount or 0)
        return (n,)


async def _drain(fn: Any, *args: Any) -> list[int]:
    totals: list[int] | None = None
    for _ in range(MAX_BATCHES_PER_PHASE):
        res = await asyncio.to_thread(fn, *args)
        totals = list(res) if totals is None else [a + b for a, b in zip(totals, res, strict=True)]
        if res[0] < BATCH_ROWS:
            break
        await asyncio.sleep(BATCH_PAUSE_SECONDS)  # 批间让出：写锁空出来给实时入库/判定
    return totals or []


# ================================================================ 清理：一轮


_RUN_LOCKS: dict[int, threading.Lock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def _run_lock(base: Any) -> threading.Lock:
    with _RUN_LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(id(base), threading.Lock())


async def _clean_one(db: Any, stats: dict[str, int], cfg: runtime_config.RetentionConfig) -> None:
    plan = await asyncio.to_thread(_build_plan, db)
    now = time.time()
    cut_epoch = int(now) - cfg.content_days * 86400
    cut_iso = iso_utc(datetime.fromtimestamp(cut_epoch, UTC))
    todo_iso = iso_utc(datetime.fromtimestamp(now - DONE_TODO_KEEP_DAYS * 86400, UTC))

    if plan.chat:                                     # 1) 对话
        stats["chat_messages"] += (await _drain(_chat_batch, db, cut_iso))[0]
    if plan.msgs:                                     # 2) 消息（+判定）
        n, dec = await _drain(_msg_batch, db, plan, cut_epoch)
        stats["messages"] += n
        stats["decisions"] += dec
    if plan.block_time_col:                           #    消息块（+按块挂的判定）
        n, dec = await _drain(_block_batch, db, plan, cut_epoch, cut_iso)
        stats["message_blocks"] += n
        stats["decisions"] += dec
    if plan.todos:                                    # 3) 已完成/不用做 90 天的待办
        stats["todo_items"] += (await _drain(_todo_batch, db, plan, todo_iso))[0]


def _write_mlog(base: Any, detail: dict, ok: bool) -> None:
    body = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    try:
        from .maintenance import mlog

        mlog.write(base, kind=MAINT_CLEANUP, detail=body, ok=ok)
        return
    except Exception as exc:  # 签名不符或其它失败 → 直接落表，维护记录不能丢
        log.warning("mlog.write 失败，改为直接写表：%s", type(exc).__name__)
    try:
        with base.write() as s:
            s.execute(text("INSERT INTO maintenance_log(kind, detail, ok, created_at) VALUES(:k, :d, :o, :t)"),
                      {"k": MAINT_CLEANUP, "d": body, "o": 1 if ok else 0, "t": utc_now_iso()})
    except Exception:
        log.exception("写维护记录失败")


async def run_all(db: Any, *, trigger: str = "manual") -> dict:
    """对主库 + 名册里每个账号库各清一轮。手动与定时同一实现；同一时刻只跑一轮（后来者等待）。"""
    from . import accounts

    base = accounts.global_db(db)
    lock = _run_lock(base)
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.1)
    try:
        cfg = await asyncio.to_thread(runtime_config.load_retention, base)
        stats = {"chat_messages": 0, "messages": 0, "decisions": 0, "message_blocks": 0, "todo_items": 0}
        errors: list[str] = []

        targets: list[tuple[str, Any]] = [("base", base)]
        seen = {id(base)}
        for acc_id in await asyncio.to_thread(accounts.list_ids, base):
            try:
                d = await asyncio.to_thread(accounts.db_for, base, acc_id)
            except accounts.AccountNotFound:  # 并发删号
                continue
            if id(d) not in seen:
                seen.add(id(d))
                targets.append((f"account:{acc_id}", d))

        for label, d in targets:
            try:
                await _clean_one(d, stats, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("清理 %s 失败：%s", label, type(exc).__name__)
                errors.append(f"{label}: {type(exc).__name__}")

        detail = {"trigger": trigger, "accounts": len(targets), "content_days": cfg.content_days,
                  "done_keep_days": DONE_TODO_KEEP_DAYS, **stats, "errors": errors}
        ok = not errors
        await asyncio.to_thread(_write_mlog, base, detail, ok)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(runtime_config.set_raw, base, RETENTION_LAST_RUN_KEY, utc_now_iso())
        log.info("保留期清理完成 %s", detail)
        return {"ok": ok, **detail}
    finally:
        lock.release()


class CleanupScheduler:
    """挂在判定循环里：tick() 不阻塞，到点就起一个独立任务跑清理（不拖慢判定）。"""

    def __init__(self, db: Any, *, first_delay: float = FIRST_RUN_DELAY_SECONDS,
                 interval: float = RUN_INTERVAL_SECONDS) -> None:
        self._db = db
        self._interval = interval
        self._next = time.monotonic() + first_delay
        self._task: asyncio.Task | None = None

    def tick(self) -> None:
        if self._task is not None and not self._task.done():
            return
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + RETRY_SECONDS  # 失败兜底；成功/跳过时 _run 会改写
        self._task = asyncio.create_task(self._run(), name="retention-cleanup")

    async def _run(self) -> None:
        try:
            raw = await asyncio.to_thread(runtime_config.get_raw, self._db, RETENTION_LAST_RUN_KEY)
            last = parse_ts(raw, UTC) if raw else None
            if last is not None:
                age = (datetime.now(UTC) - last).total_seconds()
                if 0 <= age < self._interval:  # 重启不重跑：距上一轮不足一天就顺延
                    self._next = time.monotonic() + (self._interval - age)
                    return
            await run_all(self._db, trigger="scheduled")
            self._next = time.monotonic() + self._interval
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("定时清理失败：%s", type(exc).__name__)

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task


# ================================================================ /api/settings 接入 retention.*


async def _read_body(receive: Any) -> bytes | None:
    chunks: list[bytes] = []
    while True:
        m = await receive()
        if m["type"] == "http.disconnect":
            return None
        chunks.append(m.get("body", b""))
        if not m.get("more_body"):
            return b"".join(chunks)


def _replay(body: bytes, receive: Any) -> Any:
    sent = False

    async def _r() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return _r


def _authorized(scope: dict, ctx: Any) -> bool:
    token = str(getattr(getattr(ctx, "settings", None), "access_token", "") or "")
    if not token:
        return False
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            scheme, _, cred = v.decode("latin-1").partition(" ")
            return scheme.lower() == "bearer" and hmac.compare_digest(cred.strip().encode(), token.encode())
    return False


def _merging_send(send: Any, extra: dict) -> Any:
    start: dict | None = None
    chunks: list[bytes] = []

    async def _send(message: dict) -> None:
        nonlocal start
        if message["type"] == "http.response.start":
            start = message
            return
        if message["type"] == "http.response.body" and start is not None:
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = list(start.get("headers") or [])
            ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
            if 200 <= int(start.get("status", 200)) < 300 and b"json" in ctype:
                try:
                    data = json.loads(body)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    data.update(extra)
                    body = json.dumps(data, ensure_ascii=False).encode()
                    headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                    headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})
            start = None
            return
        await send(message)

    return _send


class RetentionSettingsMiddleware:
    """PUT /api/settings 里出现 retention.* 时由这里保存（主库 settings 表），其余键原样交给原处理函数。

    - 令牌不对：原样放行，由下游照常 401
    - 只有 retention.*：转成 GET /api/settings，返回完整设置并并入两个保留期值
    - 不含 retention.* 的请求完全不受影响（既有用例的响应形状不变）
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (scope.get("type") != "http" or scope.get("method") != "PUT"
                or str(scope.get("path") or "").rstrip("/") != "/api/settings"):
            await self.app(scope, receive, send)
            return
        body = await _read_body(receive)
        if body is None:
            return
        try:
            data = json.loads(body) if body else None
        except ValueError:
            data = None
        app_obj = scope.get("app")
        ctx = getattr(getattr(app_obj, "state", None), "ctx", None)
        if (not isinstance(data, dict) or not any(k in data for k in RETENTION_SETTING_KEYS)
                or ctx is None or getattr(ctx, "db", None) is None or not _authorized(scope, ctx)):
            await self.app(scope, _replay(body, receive), send)
            return

        picked = {k: data.pop(k) for k in RETENTION_SETTING_KEYS if k in data}
        try:
            await asyncio.to_thread(runtime_config.apply_retention_update, ctx.db, picked)
        except ValueError as exc:
            from starlette.responses import JSONResponse

            await JSONResponse({"detail": f"保存设置失败：{exc}"}, status_code=400)(scope, receive, send)
            return
        cfg = await asyncio.to_thread(runtime_config.load_retention, ctx.db)

        new_body = json.dumps(data, ensure_ascii=False).encode() if data else b""
        headers = [(k, v) for k, v in scope.get("headers") or [] if k != b"content-length"]
        if new_body:
            headers.append((b"content-length", str(len(new_body)).encode()))
        new_scope = {**scope, "headers": headers, "method": "PUT" if new_body else "GET"}
        await self.app(new_scope, _replay(new_body, receive), _merging_send(send, retention_view(cfg)))
