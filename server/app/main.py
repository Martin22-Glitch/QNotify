"""应用组装入口。

启动硬约束：
- ``create_app(settings=None) -> FastAPI`` 必须存在，且接受显式传入的 Settings。
- 所有业务路由挂在 ``/api`` 下，且除 ``/healthz`` 外都要 Bearer 令牌鉴权。
- 启动/关闭副作用只能放在 lifespan 里，不要放在模块导入时（否则测试会污染真实数据）。
- 只读：不出现任何写操作类 OneBot 动作。
- 维护能力（备份/校验/恢复）不通过 HTTP 暴露：启动期兜底在 lifespan，人工恢复使用维护工具（见 app/maintenance/restore.py）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .m2 import auto_loop as m2_auto_loop
from .m2 import install as install_m2

ACCOUNT_HEADER = "X-QNotify-Account"

API_DESCRIPTION = """
多账号作用域（唯一口径）：显式 `?account=<id>`（或请求头 `X-QNotify-Account`）→ 协议端最后一次登录的号 → 主库归属账号。

- `?account=` 对读**和写**都生效（维护用，例如给另一个号补规则）；账号不存在 / 不合法返回 404（在令牌校验之后）。
- `/api/napcat/sync`、`/api/napcat/backfill` 只写协议端登录号的库；显式账号与登录号不一致返回 409，拿不到登录态返回 503。
- 模型档案、系统设置、调用账本是通用资产，任何作用域下读写都落主库。
- 备份 / 恢复不提供 HTTP 接口；定时备份在后台运行，人工恢复使用 `app/maintenance/restore.py`。
"""


class AccountParamMiddleware:
    """纯 ASGI：只把 ?account= / X-QNotify-Account 的原始值搬进上下文，**不查库、不返回响应**。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from . import accounts

        raw_token = accounts.set_request_account_raw(_account_param(scope))
        scope_token = accounts.bind_scope_account(None)
        try:
            await self.app(scope, receive, send)
        finally:
            accounts.reset_scope_account(scope_token)
            accounts.reset_request_account_raw(raw_token)


def _account_param(scope: dict) -> str | None:
    qs = scope.get("query_string") or b""
    if qs:
        vals = parse_qs(qs.decode("latin-1")).get("account")
        if vals and vals[0].strip():
            return vals[0]
    want = ACCOUNT_HEADER.lower().encode("latin-1")
    for k, v in scope.get("headers") or []:
        if k == want:
            s = v.decode("latin-1").strip()
            return s or None
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    from . import accounts, pipeline, runtime_config, sync
    from .api import (
        routes_accounts,
        routes_groups,
        routes_login,
        routes_messages,
        routes_selfcheck,
        routes_sync,
        routes_whitelist,
    )
    from .api.deps import account_scope
    from .auth import require_token
    from .context import AppContext
    from .db import Database
    from .events import hub
    from .maintenance import integrity, mlog
    from .maintenance import restore as restore_mod
    from .maintenance.scheduler import backup_loop
    from .napcat.client import NapCatClient

    log = logging.getLogger("app")
    ctx = AppContext(settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 0. 排队的整套恢复：必须在任何 Database.open 之前。内部自行处理失败，这里只兜底不让它挡启动
        try:
            await asyncio.to_thread(restore_mod.apply_pending, settings)
        except Exception:
            log.exception("启动恢复出现未预期错误；标记保留，按现状启动")

        # 1. 建表/迁移 + 名册 + 维护记录表 + 上次恢复结果入账
        db = Database(settings.db_file)
        await asyncio.to_thread(db.open)
        await asyncio.to_thread(accounts.ensure_roster, db)
        await asyncio.to_thread(mlog.ensure_table, db)
        await asyncio.to_thread(restore_mod.consume_result, settings, db)
        # 1.2 画像一次性迁移：主库与每个账号库各跑一次，幂等
        try:
            from . import m2 as m2mod

            await asyncio.to_thread(m2mod.migrate_profile_about, db)
            for acc_id in await asyncio.to_thread(accounts.list_ids, db):
                try:
                    acc_db = await asyncio.to_thread(accounts.db_for, db, acc_id)
                except accounts.AccountNotFound:
                    continue
                await asyncio.to_thread(m2mod.migrate_profile_about, acc_db)
        except Exception:
            log.exception("画像迁移失败（about），已跳过")
        ctx.db = db

        # 1.5 启动自检：user_version / quick_check / 表归类。只检查、只记录，不挡启动
        try:
            app.state.integrity = await asyncio.to_thread(integrity.run, db)
        except Exception:
            log.exception("启动自检本身出错（不影响启动）")
            app.state.integrity = {"ok": False, "checked": 0, "problems": [{"issue": "selfcheck_crashed"}]}

        # 2. NapCat WS 客户端（后台任务，不阻塞启动）
        async def on_event(frame: dict[str, Any]) -> None:
            await asyncio.to_thread(pipeline.handle_live_event, db, frame)

        client = NapCatClient(settings.adapter_ws_url, settings.adapter_token, on_event=on_event)
        ctx.napcat = client
        accounts.bind_live_source(db, lambda: accounts.live_self_id_of(client))
        hub.set_account_resolver(accounts.scope_account)
        client.start()

        # 3. 可选启动补采
        try:
            cfg = await asyncio.to_thread(runtime_config.load, db, settings)
            if cfg.backfill_enabled:
                ctx.tasks.append(
                    asyncio.create_task(sync.startup_backfill(db, client, settings), name="startup-backfill")
                )
        except Exception:
            log.exception("读取补采配置失败，跳过启动补采")

        ctx.tasks.append(asyncio.create_task(m2_auto_loop(db, ctx), name="m2-auto-flush"))
        # 4. 定时备份（兜底能力，保留；异常只记录，见 scheduler）
        ctx.tasks.append(asyncio.create_task(backup_loop(db, settings), name="scheduled-backup"))

        try:
            yield
        finally:
            for t in ctx.tasks:
                t.cancel()
            for t in ctx.tasks:
                with contextlib.suppress(BaseException):
                    await t
            ctx.tasks.clear()
            await client.stop()
            ctx.napcat = None
            accounts.bind_live_source(db, None)
            await asyncio.to_thread(accounts.dispose_all, db)
            db.dispose()
            ctx.db = None

    app = FastAPI(title="QNotify Core", version=__version__, description=API_DESCRIPTION, lifespan=lifespan)
    app.state.ctx = ctx
    app.state.integrity = None

    app.add_middleware(AccountParamMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", ACCOUNT_HEADER],
        expose_headers=["*"],
        max_age=600,
    )

    @app.exception_handler(accounts.AccountNotFound)
    async def _account_not_found(request, exc: accounts.AccountNotFound):  # type: ignore[no-untyped-def]
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token), Depends(account_scope)])
    for module in (
        routes_accounts,
        routes_login,
        routes_sync,
        routes_groups,
        routes_whitelist,
        routes_messages,
        routes_selfcheck,
    ):
        api.include_router(module.router)
    app.include_router(api)

    install_m2(app)

    return app
