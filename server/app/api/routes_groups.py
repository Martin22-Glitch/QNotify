"""GET /api/groups、/api/groups/{group_id}/members、/api/friends"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import repo
from ..db import Database
from ..models import MODE_ALL, MODE_MEMBERS
from ..schemas import FriendOut, GroupOut, MemberOut
from .deps import get_db

router = APIRouter()


def _matches(q: str | None, *fields: str) -> bool:
    if q is None:
        return True
    needle = q.strip().casefold()
    if not needle:
        return True
    return any(needle in (f or "").casefold() for f in fields)


@router.get("/groups", response_model=list[GroupOut])
def list_groups(q: str | None = None, db: Database = Depends(get_db)) -> list[GroupOut]:
    with db.session() as s:
        rows = repo.list_groups(s)
        wl = repo.load_whitelist(s)
    out: list[GroupOut] = []
    for r in rows:
        if not _matches(q, r["group_name"]):
            continue
        gid = int(r["group_id"])
        mode = wl.groups.get(gid)
        member_ids = sorted(wl.members.get(gid, frozenset())) if mode == MODE_MEMBERS else []
        out.append(
            GroupOut(
                group_id=gid,
                group_name=r["group_name"],
                member_count=int(r["member_count"]),
                whitelisted=mode is not None,
                mode=mode,
                whitelisted_member_ids=member_ids,
            )
        )
    return out


@router.get("/groups/{group_id}/members", response_model=list[MemberOut])
def list_members(group_id: int, q: str | None = None, db: Database = Depends(get_db)) -> list[MemberOut]:
    with db.session() as s:
        if not repo.group_exists(s, group_id):
            raise HTTPException(status_code=404, detail="群不存在")
        rows = repo.list_members(s, group_id)
        wl = repo.load_whitelist(s)
    mode = wl.groups.get(group_id)
    wl_members = wl.members.get(group_id, frozenset())
    out: list[MemberOut] = []
    for r in rows:
        if not _matches(q, r["nickname"], r["card"]):
            continue
        uid = int(r["user_id"])
        whitelisted = mode == MODE_ALL or (mode == MODE_MEMBERS and uid in wl_members)
        out.append(
            MemberOut(
                user_id=uid,
                nickname=r["nickname"],
                card=r["card"],
                role=r["role"],
                whitelisted=whitelisted,
            )
        )
    return out


@router.get("/friends", response_model=list[FriendOut])
def list_friends(q: str | None = None, db: Database = Depends(get_db)) -> list[FriendOut]:
    with db.session() as s:
        rows = repo.list_friends(s)
        wl = repo.load_whitelist(s)
    out: list[FriendOut] = []
    for r in rows:
        if not _matches(q, r["nickname"], r["remark"]):
            continue
        uid = int(r["user_id"])
        out.append(
            FriendOut(user_id=uid, nickname=r["nickname"], remark=r["remark"], whitelisted=uid in wl.users)
        )
    return out
