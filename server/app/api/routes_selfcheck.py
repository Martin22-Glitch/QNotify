"""GET /api/selfcheck：固定检查项，单项失败不影响整体返回。

注意：这里检查的"白名单/管线配置"属于**当前账号**，所以库要从 accounts 按作用域取，
不能用主库（否则换号后状态页显示的是上一个号的白名单）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends

from .. import repo, runtime_config
from ..context import AppContext
from ..db import Database
from ..schemas import SelfCheckItem, SelfCheckOut
from .deps import get_ctx

router = APIRouter()
log = logging.getLogger(__name__)


def _safe(name: str, fn: Callable[[], tuple[bool, str]]) -> SelfCheckItem:
    try:
        ok, detail = fn()
    except Exception as exc:
        log.warning("自检 %s 异常：%s", name, type(exc).__name__)
        ok, detail = False, f"检查异常：{type(exc).__name__}"
    return SelfCheckItem(name=name, ok=ok, detail=detail)


def run_checks(ctx: AppContext, db: Database | None = None) -> SelfCheckOut:
    """自检的纯逻辑：db 传当前账号的库；不传就按请求作用域自己取。"""
    settings = ctx.settings
    target = db
    if target is None and ctx.db is not None:
        from .. import accounts

        target = accounts.db_for_scope(ctx.db, getattr(ctx.napcat, "self_id", None))

    def db_writable() -> tuple[bool, str]:
        if target is None:
            return False, "数据库未打开"
        target.probe_writable()
        return True, f"{settings.db_path} 可写"

    def napcat_connected() -> tuple[bool, str]:
        if ctx.napcat is not None and ctx.napcat.connected:
            return True, f"{settings.adapter_ws_url} 已连接"
        return False, f"{settings.adapter_ws_url} 未连接"

    def napcat_login() -> tuple[bool, str]:
        if ctx.napcat is not None and ctx.napcat.logged_in:
            return True, f"self_id={ctx.napcat.self_id}"
        return False, "未获取到登录态"

    def whitelist_configured() -> tuple[bool, str]:
        if target is None:
            return False, "数据库未打开"
        with target.session() as s:
            wl = repo.load_whitelist(s)
        if wl.is_empty:
            return False, "白名单为空，不会处理任何消息"
        return True, f"白名单群 {len(wl.groups)} 个，私聊 {len(wl.users)} 人"

    def backfill_enabled() -> tuple[bool, str]:
        if target is None:
            return False, "数据库未打开"
        cfg = runtime_config.load(target, settings)
        if cfg.backfill_enabled:
            return True, f"每群补采 {cfg.backfill_count} 条"
        return False, "启动自动补采已关闭"

    checks = [
        _safe("db_writable", db_writable),
        _safe("napcat_connected", napcat_connected),
        _safe("napcat_login", napcat_login),
        _safe("whitelist_configured", whitelist_configured),
        _safe("backfill_enabled", backfill_enabled),
    ]
    return SelfCheckOut(ok=all(c.ok for c in checks), checks=checks)


@router.get("/selfcheck", response_model=SelfCheckOut)
def selfcheck(ctx: AppContext = Depends(get_ctx)) -> SelfCheckOut:
    return run_checks(ctx)
