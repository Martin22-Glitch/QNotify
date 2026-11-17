"""sync（群/成员/好友）与 backfill（补采）业务流程。

账号：一律写进**协议端此刻登录的号**对应的库。登录号不在名册里就当场建；
拿不到登录态 → SyncError(503)，绝不退回主库归属账号写数据。
补采跳过早于保留期的消息（截止线与实时入库、清理同一口径）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from . import accounts, gate, ingest, repo, runtime_config
from .config import Settings
from .db import Database, utc_now_iso
from .models import MEMBER_ROLES, VIA_BACKFILL, WhitelistSnapshot
from .napcat import events
from .napcat.client import NapCatClient
from .napcat.events import as_int

log = logging.getLogger(__name__)

CONNECT_WAIT_SECONDS = 5.0
LOGIN_WAIT_SECONDS = 5.0


class SyncError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def _require_connected(client: NapCatClient | None) -> NapCatClient:
    if client is None or not await client.wait_connected(CONNECT_WAIT_SECONDS):
        raise SyncError(503, "NapCat 未连接，请检查 QN_ADAPTER_WS_URL 与 NapCat 是否在运行")
    return client


async def resolve_login_target(
    db: Database, client: NapCatClient | None, *, explicit_account_id: int | None = None
) -> tuple[NapCatClient, int, Database]:
    """确定写入目标：协议端此刻登录的号 → 名册行（没有就建）→ 账号库。"""
    client = await _require_connected(client)
    with contextlib.suppress(Exception):
        await client.wait_logged_in(LOGIN_WAIT_SECONDS)
    sid = as_int(getattr(client, "self_id", None))
    if sid is None:
        raise SyncError(503, "协议端未返回登录账号（get_login_info），为避免写错账号，本次不写入")

    base = accounts.global_db(db)
    nickname = str(getattr(client, "nickname", "") or "")
    acc = await asyncio.to_thread(accounts.touch, base, sid, nickname)
    acc_id = acc.get("id")
    if acc_id is None:
        raise SyncError(503, f"登录账号 {sid} 建名册失败")
    acc_id = int(acc_id)
    if explicit_account_id is not None and explicit_account_id != acc_id:
        raise SyncError(
            409, f"同步只能写协议端登录的号（账号 {acc_id}），与指定的账号 {explicit_account_id} 不一致"
        )
    target = await asyncio.to_thread(accounts.db_for, base, acc_id)
    return client, acc_id, target


# ---------------------------------------------------------------- sync


def _write_contacts(
    db: Database,
    groups: list[dict[str, Any]],
    members: list[dict[str, Any]],
    friends: list[dict[str, Any]],
) -> None:
    now = utc_now_iso()
    with db.write() as s:
        repo.upsert_groups(s, groups, now)
        repo.upsert_members(s, members, now)
        repo.upsert_friends(s, friends, now)


async def sync_contacts(
    db: Database, client: NapCatClient | None, *, explicit_account_id: int | None = None
) -> dict[str, int]:
    client, acc_id, target = await resolve_login_target(db, client, explicit_account_id=explicit_account_id)

    group_data = await client.call("get_group_list")
    if not isinstance(group_data, list):
        raise SyncError(502, "拉取群列表失败（get_group_list 无有效响应）")

    groups: dict[int, dict[str, Any]] = {}
    for g in group_data:
        if not isinstance(g, dict):
            continue
        gid = as_int(g.get("group_id"))
        if gid is None:
            continue
        groups[gid] = {
            "group_id": gid,
            "group_name": str(g.get("group_name") or ""),
            "member_count": as_int(g.get("member_count")) or 0,
        }

    members: dict[tuple[int, int], dict[str, Any]] = {}
    for gid in groups:
        data = await client.call("get_group_member_list", {"group_id": gid})
        if not isinstance(data, list):
            log.warning("拉取群成员失败，跳过 group=%s", gid)
            continue
        fetched = 0
        for m in data:
            if not isinstance(m, dict):
                continue
            uid = as_int(m.get("user_id"))
            if uid is None:
                continue
            role = str(m.get("role") or "member")
            members[(gid, uid)] = {
                "group_id": gid,
                "user_id": uid,
                "nickname": str(m.get("nickname") or ""),
                "card": str(m.get("card") or ""),
                "role": role if role in MEMBER_ROLES else "member",
            }
            fetched += 1
        if groups[gid]["member_count"] == 0 and fetched:
            groups[gid]["member_count"] = fetched

    friend_data = await client.call("get_friend_list")
    friends: dict[int, dict[str, Any]] = {}
    if isinstance(friend_data, list):
        for f in friend_data:
            if not isinstance(f, dict):
                continue
            uid = as_int(f.get("user_id"))
            if uid is None:
                continue
            friends[uid] = {
                "user_id": uid,
                "nickname": str(f.get("nickname") or ""),
                "remark": str(f.get("remark") or ""),
            }
    else:
        log.warning("拉取好友列表失败（get_friend_list 无有效响应）")

    await asyncio.to_thread(
        _write_contacts, target, list(groups.values()), list(members.values()), list(friends.values())
    )
    log.info("同步完成 account=%s groups=%s members=%s friends=%s",
             acc_id, len(groups), len(members), len(friends))
    return {"groups": len(groups), "members": len(members), "friends": len(friends)}


# ---------------------------------------------------------------- backfill


def _load_whitelist(db: Database) -> WhitelistSnapshot:
    with db.session() as s:
        return repo.load_whitelist(s)


def _extract_history(data: Any) -> list[Any] | None:
    if isinstance(data, dict):
        msgs = data.get("messages")
        return msgs if isinstance(msgs, list) else None
    if isinstance(data, list):
        return data
    return None


def _ingest_history(db: Database, group_id: int, items: list[Any], wl: WhitelistSnapshot) -> int:
    # 截止线每批只取一次（不走缓存，刚改过的保留期立即生效）
    cutoff = runtime_config.content_cutoff_epoch(db, max_age=0.0)
    inserted = 0
    too_old = 0
    for item in items:
        msg = events.parse_history(item, group_id)
        if msg is None:
            continue
        if 0 < int(msg.time or 0) < cutoff:  # time 缺失(<=0)不拦，交给后面
            too_old += 1
            continue
        decision = gate.check(msg, wl)
        if not decision.allowed:
            log.debug("补采闸门过滤 msg_id=%s group=%s reason=%s", msg.msg_id, group_id, decision.reason)
            continue
        if ingest.write(db, msg, VIA_BACKFILL):
            inserted += 1
    if too_old:
        log.info("补采跳过早于保留期的消息 group=%s count=%s", group_id, too_old)
    return inserted


async def backfill(
    db: Database, client: NapCatClient | None, count: int, *, explicit_account_id: int | None = None
) -> dict[str, int]:
    client, acc_id, target = await resolve_login_target(db, client, explicit_account_id=explicit_account_id)
    wl = await asyncio.to_thread(_load_whitelist, target)
    if not wl.groups:
        return {"groups": 0, "scanned": 0, "inserted": 0}

    groups_done = 0
    scanned = 0
    inserted = 0
    for gid in sorted(wl.groups):
        data = await client.call("get_group_msg_history", {"group_id": gid, "count": count})
        items = _extract_history(data)
        if items is None:
            log.warning("补采失败，跳过 group=%s", gid)
            continue
        groups_done += 1
        scanned += len(items)
        inserted += await asyncio.to_thread(_ingest_history, target, gid, items, wl)
    log.info("补采完成 account=%s groups=%s scanned=%s inserted=%s", acc_id, groups_done, scanned, inserted)
    return {"groups": groups_done, "scanned": scanned, "inserted": inserted}


async def startup_backfill(db: Database, client: NapCatClient, settings: Settings) -> None:
    """lifespan 启动的一次性后台补采；拿不到登录态就跳过（只记日志），不写任何库。"""
    try:
        if not await client.wait_connected(60):
            log.warning("启动补采跳过：60s 内未连上 NapCat")
            return
        await client.wait_logged_in(10)
        cfg = await asyncio.to_thread(runtime_config.load, db, settings)
        await backfill(db, client, cfg.backfill_count)
    except asyncio.CancelledError:
        raise
    except SyncError as exc:
        log.warning("启动补采失败：%s", exc.detail)
    except Exception:
        log.exception("启动补采异常")
