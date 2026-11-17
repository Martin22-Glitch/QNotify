"""GET/PUT /api/whitelist（整体替换；校验失败一条都不写）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import repo
from ..db import Database, utc_now_iso
from ..models import MODE_ALL, MODE_MEMBERS, WHITELIST_MODES
from ..schemas import WhitelistIn, WhitelistOut
from .deps import get_db

router = APIRouter()


class WhitelistInvalid(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def validate_plan(s, body: WhitelistIn) -> tuple[list[tuple[int, str, list[int]]], list[int]]:
    """在同一事务内校验，返回待写入计划；任一非法抛 WhitelistInvalid。"""
    plan: list[tuple[int, str, list[int]]] = []
    seen: set[int] = set()
    for g in body.groups or []:
        if g.group_id in seen:
            raise WhitelistInvalid(f"群 {g.group_id} 重复出现")
        seen.add(g.group_id)
        if not repo.group_exists(s, g.group_id):
            raise WhitelistInvalid(f"群 {g.group_id} 不在缓存中，请先同步")
        if g.mode not in WHITELIST_MODES:
            raise WhitelistInvalid(f"群 {g.group_id} 的模式 {g.mode!r} 非法，只能是 all 或 members")
        if g.mode == MODE_ALL:
            plan.append((g.group_id, MODE_ALL, []))
            continue
        ids = sorted(set(g.member_ids or []))
        if not ids:
            raise WhitelistInvalid(f"群 {g.group_id} 为仅指定成员模式，但成员列表为空")
        in_group = repo.group_member_ids(s, g.group_id)
        missing = [uid for uid in ids if uid not in in_group]
        if missing:
            raise WhitelistInvalid(f"群 {g.group_id} 中不存在成员：{missing}")
        plan.append((g.group_id, MODE_MEMBERS, ids))
    users = sorted(set(body.users or []))
    return plan, users


@router.get("/whitelist", response_model=WhitelistOut)
def get_whitelist(db: Database = Depends(get_db)) -> WhitelistOut:
    with db.session() as s:
        return WhitelistOut(**repo.whitelist_view(s))


@router.put("/whitelist", response_model=WhitelistOut)
def put_whitelist(body: WhitelistIn, db: Database = Depends(get_db)) -> WhitelistOut:
    try:
        with db.write() as s:
            plan, users = validate_plan(s, body)
            repo.replace_whitelist(s, plan, users, utc_now_iso())
    except WhitelistInvalid as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    with db.session() as s:
        return WhitelistOut(**repo.whitelist_view(s))
