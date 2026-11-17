"""路由依赖：取上下文、取数据库、鉴权、作用域。

作用域（唯一口径）：显式 ``?account=`` / ``X-QNotify-Account`` → 协议端最后一次登录的号 → 主库归属账号。

``?account=`` 对**读写都生效**（维护用：例如在另一个号的库里补规则）。不存在的账号一律 404，
且 404 一定发生在令牌校验之后（``account_scope`` 挂在 /api 路由的 require_token 之后）。
同步 / 补采例外：它们只写协议端登录号的库，显式账号与登录号不一致时 409。

没走 ``account_scope`` 的路由（m2 自己注册的那批）用 ``bind_scope_quietly`` 绑定作用域，
口径相同，但从不抛错：它只负责让本请求里的 hub.publish 能自动补 account_id，
404 / 503 等仍由原业务路径照旧抛出，既有响应不变。
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection

from ..auth import require_token
from ..context import AppContext
from ..db import Database

__all__ = ["get_ctx", "get_db", "get_base_db", "account_scope", "bind_scope_quietly", "require_token"]


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_base_db(ctx: AppContext = Depends(get_ctx)) -> Database:
    """主库：账号名册、通用资产（模型档案 / 系统设置 / 账本）都在这里。"""
    if ctx.db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    return ctx.db


async def _resolve_and_bind(ctx: AppContext) -> int | None:
    """算作用域并绑到本请求上下文（contextvar）。查库放线程池，不阻塞事件循环。"""
    from .. import accounts

    acc_id = await asyncio.to_thread(
        accounts.resolve_scope_id, ctx.db, accounts.live_self_id_of(ctx.napcat)
    )
    accounts.bind_scope_account(acc_id)  # 请求结束由 ASGI 中间件复位
    return acc_id


async def account_scope(ctx: AppContext = Depends(get_ctx)) -> int | None:
    """鉴权之后算一次作用域并绑到本请求上下文。

    绑定值同时用于：后续 get_db 取库、本请求里 hub.publish 自动补 account_id。
    """
    if ctx.db is None:
        return None
    return await _resolve_and_bind(ctx)


async def bind_scope_quietly(conn: HTTPConnection) -> None:
    """路由器级兜底：给没挂 account_scope 的路由绑定同口径的作用域，失败不抛。

    - 用 HTTPConnection 而不是 Request：挂到含 WebSocket 路由的路由器上也不会出错；
    - 吞掉异常（例如显式账号不存在的 404）：错误留给原来的取库路径照旧抛，响应不变；
      这种情况下不绑定，publish 就退回"没有账号上下文"。
    """
    ctx = getattr(conn.app.state, "ctx", None)
    if ctx is None or getattr(ctx, "db", None) is None:
        return
    try:
        await _resolve_and_bind(ctx)
    except Exception:
        return


def get_db(ctx: AppContext = Depends(get_ctx)) -> Database:
    """业务库：当前作用域对应的账号库实例。"""
    base = get_base_db(ctx)
    from .. import accounts

    return accounts.db_for_scope(base, accounts.live_self_id_of(ctx.napcat))
