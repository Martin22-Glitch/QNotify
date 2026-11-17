"""POST /api/napcat/sync、POST /api/napcat/backfill

只写协议端登录号的库；``?account=`` 与登录号不一致 → 409；拿不到登录态 → 503。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from .. import accounts, runtime_config, sync
from ..context import AppContext
from ..db import Database
from ..schemas import BackfillResult, SyncResult
from .deps import get_base_db, get_ctx

router = APIRouter()


@router.post("/napcat/sync", response_model=SyncResult)
async def napcat_sync(
    ctx: AppContext = Depends(get_ctx), base: Database = Depends(get_base_db)
) -> SyncResult:
    explicit = await asyncio.to_thread(accounts.explicit_account_id, base)
    try:
        result = await sync.sync_contacts(base, ctx.napcat, explicit_account_id=explicit)
    except sync.SyncError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return SyncResult(**result)


@router.post("/napcat/backfill", response_model=BackfillResult)
async def napcat_backfill(
    ctx: AppContext = Depends(get_ctx), base: Database = Depends(get_base_db)
) -> BackfillResult:
    explicit = await asyncio.to_thread(accounts.explicit_account_id, base)
    cfg = await asyncio.to_thread(runtime_config.load, base, ctx.settings)
    try:
        result = await sync.backfill(base, ctx.napcat, cfg.backfill_count, explicit_account_id=explicit)
    except sync.SyncError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return BackfillResult(**result)
