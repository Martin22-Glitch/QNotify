"""GET/PATCH /api/accounts（账号名册）。

**没有"切换当前账号"**：唯一的"当前"是协议端登录态（``is_live``）。
前端想看另一个号，就在请求上带 ``?account=``。

**没有"删账号"**：换号由协议端负责；系统内不提供任何删除账号数据的入口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import accounts
from ..context import AppContext
from ..db import Database
from .deps import get_base_db, get_ctx

router = APIRouter()


class PatchIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, pattern="^(test|main)$")


@router.get("/accounts")
def list_accounts(
    db: Database = Depends(get_base_db), ctx: AppContext = Depends(get_ctx)
) -> list[dict]:
    """账号列表。``is_live`` = 协议端最后一次登录的号（断线退避期间不变，与默认作用域同一来源）。"""
    live = accounts.live_self_id_of(ctx.napcat)
    rows = accounts.list_all(db)
    for r in rows:
        r["is_live"] = bool(live is not None and r.get("self_id") == live)
    return rows


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: PatchIn, db: Database = Depends(get_base_db)) -> dict:
    if accounts.get(db, account_id) is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row = accounts.rename(db, account_id, nickname=body.nickname, role=body.role)
    return row or {}
@router.delete("/accounts/{account_id}", include_in_schema=False)
def delete_account_removed(account_id: int) -> None:
    """删账号功能已移除。显式回 404（否则同路径有 PATCH，框架会回 405）。"""
    raise HTTPException(status_code=404, detail="Not Found")
