"""设置读写实现（路由由 app/m2.py 注册）。"""

from __future__ import annotations

from fastapi import Depends

from .. import runtime_config
from ..context import AppContext
from ..db import Database
from ..schemas import SettingsOut, SettingsUpdate
from .deps import get_base_db, get_ctx


def _view(db: Database, ctx: AppContext) -> SettingsOut:
    cfg = runtime_config.load(db, ctx.settings)
    return SettingsOut(**runtime_config.public_view(cfg, ctx.settings))


def get_settings(ctx: AppContext = Depends(get_ctx), db: Database = Depends(get_base_db)) -> SettingsOut:
    return _view(db, ctx)


def put_settings(
    body: SettingsUpdate, ctx: AppContext = Depends(get_ctx), db: Database = Depends(get_base_db)
) -> SettingsOut:
    runtime_config.apply_update(db, body.model_dump(exclude_unset=True))
    return _view(db, ctx)
