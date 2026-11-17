"""适配器的 HTTP 面：

- GET /api/adapter/info
- GET /api/selfcheck：在 M1 原有结果上合并 adapter_* 新项，并保证与 napcat_* 旧项一致
- 能力闸门（纯 ASGI 中间件，位于 CORS 之内）：
  * PUT /api/whitelist 含 mode="members" 且协议端没有声明 members → 400
  * 补采接口在协议端没有声明 history 时 → 返回全 0
挂载：由 m2.install() 调用本模块的 install(app)。
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.adapters.base import CAP_HISTORY, CAP_MEMBERS, UNSUPPORTED_DETAIL, Capabilities
from app.adapters.config import AdapterConfig, apply_to_settings, resolve_adapter_config
from app.adapters.registry import (
    adapter_type,
    default_capabilities,
    get_adapter,
    normalize_name,
    reconfigure_existing,
)
from app.auth import require_token, token_matches
from app.models import MODE_MEMBERS

log = logging.getLogger(__name__)

# (新名, 旧名)：旧名保留一个版本周期
SELFCHECK_RENAMES: tuple[tuple[str, str], ...] = (
    ("adapter_connected", "napcat_connected"),
    ("adapter_login", "napcat_login"),
)
WHITELIST_PATH = "/api/whitelist"
BACKFILL_PATH = "/api/napcat/backfill"  # 既有路由路径，暂不改名（前端下一轮）
EMPTY_BACKFILL = {"groups": 0, "scanned": 0, "inserted": 0}


# ---------------------------------------------------------------- 工具
def _resolve(settings: Any) -> AdapterConfig:
    cfg = resolve_adapter_config(settings)
    return dataclasses.replace(cfg, name=normalize_name(cfg.name))


def adapter_config(app: Any) -> AdapterConfig:
    cfg = getattr(app.state, "adapter_config", None)
    if isinstance(cfg, AdapterConfig):
        return cfg
    ctx = getattr(app.state, "ctx", None)
    cfg = _resolve(getattr(ctx, "settings", None))
    app.state.adapter_config = cfg
    return cfg


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _dump(x: Any) -> dict[str, Any]:
    if x is None:
        return {}
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if isinstance(x, dict):
        return dict(x)
    return {}


async def adapter_state(app: Any) -> dict[str, Any]:
    """当前适配器的状态快照。拿不到适配器时退回 M1 的登录状态函数。"""
    ctx = getattr(app.state, "ctx", None)
    cfg = adapter_config(app)
    adapter = get_adapter(ctx, cfg.name)
    if adapter is not None:
        login = await adapter.login_status()
        return {
            "name": adapter.name,
            "connected": bool(adapter.connected),
            "logged_in": bool(login.get("logged_in")),
            "capabilities": Capabilities(adapter.capabilities),
            "self_id": login.get("self_id"),
            "nickname": login.get("nickname"),
        }
    login: dict[str, Any] = {}
    if ctx is not None:
        try:
            from app.api import routes_login

            login = _dump(await _maybe_await(routes_login.login_status(ctx)))
        except Exception as exc:
            log.warning("读取登录状态失败：%s", type(exc).__name__)
    logged_in = bool(login.get("logged_in"))
    return {
        "name": adapter_type(cfg.name).name,
        "connected": bool(login.get("connected", logged_in)),
        "logged_in": logged_in,
        "capabilities": default_capabilities(cfg.name),
        "self_id": login.get("self_id") if logged_in else None,
        "nickname": login.get("nickname") if logged_in else None,
    }


def merge_selfcheck(checks: list[dict[str, Any]], state: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """合并新旧自检项。返回 (checks, 是否新增了失败项)。

    一致性保证：若旧项已存在，新项的 ok 跟随旧项（旧项是既有判定逻辑）；
    若旧项不存在，则用新项复制出旧项。两者永远同值。
    """
    out = [dict(c) for c in checks]
    by_name = {c.get("name"): c for c in out}
    name = state["name"]

    def detail(new: str, ok: bool) -> str:
        if new == "adapter_connected":
            return f"{name} 已连接" if ok else f"{name} 未连接"
        return f"已登录 self_id={state.get('self_id')}" if ok else "协议端未登录或未连接"

    fresh = {"adapter_connected": bool(state["connected"]), "adapter_login": bool(state["logged_in"])}
    for new, old in SELFCHECK_RENAMES:
        legacy = by_name.get(old)
        ok = bool(legacy.get("ok")) if legacy is not None else fresh[new]
        item = {"name": new, "ok": ok, "detail": detail(new, ok)}
        if new in by_name:
            by_name[new].update(item)
        else:
            out.append(item)
            by_name[new] = item
        if legacy is None:
            mirror = dict(item, name=old)
            out.append(mirror)
            by_name[old] = mirror

    added_failure = False
    if CAP_HISTORY not in state["capabilities"] and "adapter_history" not in by_name:
        out.append({"name": "adapter_history", "ok": False, "detail": UNSUPPORTED_DETAIL[CAP_HISTORY]})
        added_failure = True
    return out, added_failure


# ---------------------------------------------------------------- 路由
router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/adapter/info")
async def adapter_info(request: Request) -> dict:
    st = await adapter_state(request.app)
    return {
        "name": st["name"],
        "connected": st["connected"],
        "capabilities": st["capabilities"].ordered(),
        "self_id": st["self_id"],
        "nickname": st["nickname"],
    }


@router.get("/selfcheck")
async def selfcheck(request: Request) -> dict:
    from app.api import routes_selfcheck

    ctx = request.app.state.ctx
    # 必须把"当前账号"的库传进去：selfcheck 里的白名单计数要按账号算，
    # 否则换号后状态页显示的是上一个号的白名单（看起来像"没配却显示 1 个"）。
    from app import accounts

    db = accounts.db_for_scope(ctx.db, getattr(ctx.napcat, "self_id", None)) if ctx.db is not None else None
    base = _dump(await _maybe_await(routes_selfcheck.run_checks(ctx, db)))
    checks = [_dump(c) for c in (base.get("checks") or [])]
    merged, added_failure = merge_selfcheck(checks, await adapter_state(request.app))
    out = {**base, "checks": merged}
    if added_failure and isinstance(out.get("ok"), bool):
        out["ok"] = False
    return out


# ---------------------------------------------------------------- 能力闸门
async def _buffer(receive: Receive) -> tuple[bytes, Receive]:
    chunks: list[bytes] = []
    early: Message | None = None
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            early = message
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    replayed = False

    async def replay() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            if early is not None:
                return early
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


def _needs_members(body: bytes) -> bool:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return False
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return False
    return any(isinstance(g, dict) and g.get("mode") == MODE_MEMBERS for g in groups)


class CapabilityGateMiddleware:
    """能力缺失时明确拒绝。鉴权不通过的请求原样放行，由下游返回 401。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = (scope.get("path") or "").rstrip("/")
        if method == "PUT" and path == WHITELIST_PATH and self._authorized(scope):
            body, replay = await _buffer(receive)
            if _needs_members(body) and CAP_MEMBERS not in self._capabilities(scope):
                resp = JSONResponse({"detail": UNSUPPORTED_DETAIL[CAP_MEMBERS]}, status_code=400)
                await resp(scope, replay, send)
                return
            await self.app(scope, replay, send)
            return
        if method == "POST" and path == BACKFILL_PATH and self._authorized(scope):
            if CAP_HISTORY not in self._capabilities(scope):
                await JSONResponse(dict(EMPTY_BACKFILL))(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _authorized(scope: Scope) -> bool:
        app = scope.get("app")
        ctx = getattr(getattr(app, "state", None), "ctx", None)
        expected = getattr(getattr(ctx, "settings", None), "access_token", None) or os.environ.get(
            "QN_ACCESS_TOKEN", ""
        )
        if hasattr(expected, "get_secret_value"):
            expected = expected.get_secret_value()
        if not expected:
            return False
        header = ""
        for k, v in scope.get("headers") or []:
            if k.lower() == b"authorization":
                header = v.decode("latin-1")
                break
        scheme, _, given = header.partition(" ")
        return scheme.lower() == "bearer" and token_matches(given.strip(), expected)

    @staticmethod
    def _capabilities(scope: Scope) -> Capabilities:
        app = scope.get("app")
        try:
            cfg = adapter_config(app)
            adapter = get_adapter(getattr(app.state, "ctx", None), cfg.name)
            if adapter is not None:
                return Capabilities(adapter.capabilities)
            return default_capabilities(cfg.name)
        except Exception as exc:
            log.warning("读取适配器能力失败：%s", type(exc).__name__)
            return default_capabilities(None)


# ---------------------------------------------------------------- 挂载
def install(app: FastAPI) -> None:
    ctx = getattr(app.state, "ctx", None)
    settings = getattr(ctx, "settings", None)
    cfg = _resolve(settings)
    app.state.adapter_config = cfg
    apply_to_settings(settings, cfg)  # 过渡桥接：仍按旧字段建客户端的启动代码也拿到同一地址
    if ctx is not None:
        reconfigure_existing(ctx, cfg)  # 若客户端已在 create_app 中构造，同步地址/令牌

    n = len(app.router.routes)
    app.include_router(router)
    new = app.router.routes[n:]
    del app.router.routes[n:]
    app.router.routes[0:0] = new

    # 追加到 user_middleware 末尾 = 最内层，CORS 仍包在外面，拒绝响应也带 CORS 头
    app.user_middleware.append(Middleware(CapabilityGateMiddleware))
