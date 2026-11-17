"""SQL 查询集中地：白名单、群/成员/好友缓存、消息查询、运行时设置键值。

业务模块（gate/sync/路由）只调用这里的函数，不直接写 SQL。
消息 INSERT 在 ingest.py（因为它要配合 IntegrityError 去重）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import MODE_MEMBERS, WhitelistSnapshot

MESSAGE_COLUMNS = "id, msg_id, group_id, user_id, message_type, sender_name, time, text, via, ingested_at"

_COUNTABLE_TABLES = frozenset(
    {"messages", "groups", "members", "friends", "whitelist_groups", "whitelist_users"}
)


def count_rows(s: Session, table: str) -> int:
    if table not in _COUNTABLE_TABLES:
        raise ValueError(f"不允许统计的表：{table}")
    return int(s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


# ---------------------------------------------------------------- 白名单


def load_whitelist(s: Session) -> WhitelistSnapshot:
    groups = {int(gid): str(mode) for gid, mode in s.execute(text("SELECT group_id, mode FROM whitelist_groups"))}
    members: dict[int, set[int]] = {}
    for gid, uid in s.execute(text("SELECT group_id, user_id FROM whitelist_members")):
        members.setdefault(int(gid), set()).add(int(uid))
    users = frozenset(int(r[0]) for r in s.execute(text("SELECT user_id FROM whitelist_users")))
    return WhitelistSnapshot(
        groups=groups,
        members={gid: frozenset(uids) for gid, uids in members.items()},
        users=users,
    )


def whitelist_view(s: Session) -> dict[str, Any]:
    wl = load_whitelist(s)
    groups = []
    for gid in sorted(wl.groups):
        mode = wl.groups[gid]
        member_ids = sorted(wl.members.get(gid, frozenset())) if mode == MODE_MEMBERS else []
        groups.append({"group_id": gid, "mode": mode, "member_ids": member_ids})
    return {"groups": groups, "users": sorted(wl.users)}


def replace_whitelist(
    s: Session,
    plan: list[tuple[int, str, list[int]]],
    users: list[int],
    now: str,
) -> None:
    """整体替换。调用方负责在同一事务里先完成校验。"""
    s.execute(text("DELETE FROM whitelist_members"))
    s.execute(text("DELETE FROM whitelist_groups"))
    s.execute(text("DELETE FROM whitelist_users"))

    group_rows = [{"group_id": gid, "mode": mode, "updated_at": now} for gid, mode, _ in plan]
    if group_rows:
        s.execute(
            text("INSERT INTO whitelist_groups (group_id, mode, updated_at) VALUES (:group_id, :mode, :updated_at)"),
            group_rows,
        )
    member_rows = [{"group_id": gid, "user_id": uid} for gid, _, ids in plan for uid in ids]
    if member_rows:
        s.execute(
            text("INSERT INTO whitelist_members (group_id, user_id) VALUES (:group_id, :user_id)"),
            member_rows,
        )
    user_rows = [{"user_id": uid} for uid in users]
    if user_rows:
        s.execute(text("INSERT INTO whitelist_users (user_id) VALUES (:user_id)"), user_rows)


# ---------------------------------------------------------------- 群 / 成员 / 好友


def group_exists(s: Session, group_id: int) -> bool:
    row = s.execute(text("SELECT 1 FROM groups WHERE group_id = :gid"), {"gid": group_id}).first()
    return row is not None


def group_member_ids(s: Session, group_id: int) -> set[int]:
    rows = s.execute(text("SELECT user_id FROM members WHERE group_id = :gid"), {"gid": group_id})
    return {int(r[0]) for r in rows}


def upsert_groups(s: Session, rows: list[dict[str, Any]], now: str) -> None:
    if not rows:
        return
    s.execute(
        text(
            "INSERT INTO groups (group_id, group_name, member_count, updated_at) "
            "VALUES (:group_id, :group_name, :member_count, :updated_at) "
            "ON CONFLICT(group_id) DO UPDATE SET "
            "group_name = excluded.group_name, "
            "member_count = excluded.member_count, "
            "updated_at = excluded.updated_at"
        ),
        [{**r, "updated_at": now} for r in rows],
    )


def upsert_members(s: Session, rows: list[dict[str, Any]], now: str) -> None:
    if not rows:
        return
    s.execute(
        text(
            "INSERT INTO members (group_id, user_id, nickname, card, role, updated_at) "
            "VALUES (:group_id, :user_id, :nickname, :card, :role, :updated_at) "
            "ON CONFLICT(group_id, user_id) DO UPDATE SET "
            "nickname = excluded.nickname, "
            "card = excluded.card, "
            "role = excluded.role, "
            "updated_at = excluded.updated_at"
        ),
        [{**r, "updated_at": now} for r in rows],
    )


def upsert_friends(s: Session, rows: list[dict[str, Any]], now: str) -> None:
    if not rows:
        return
    s.execute(
        text(
            "INSERT INTO friends (user_id, nickname, remark, updated_at) "
            "VALUES (:user_id, :nickname, :remark, :updated_at) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "nickname = excluded.nickname, "
            "remark = excluded.remark, "
            "updated_at = excluded.updated_at"
        ),
        [{**r, "updated_at": now} for r in rows],
    )


def list_groups(s: Session) -> list[dict[str, Any]]:
    rows = s.execute(text("SELECT group_id, group_name, member_count FROM groups ORDER BY group_id ASC"))
    return [dict(r._mapping) for r in rows]


def list_members(s: Session, group_id: int) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            "SELECT user_id, nickname, card, role FROM members "
            "WHERE group_id = :gid ORDER BY user_id ASC"
        ),
        {"gid": group_id},
    )
    return [dict(r._mapping) for r in rows]


def list_friends(s: Session) -> list[dict[str, Any]]:
    rows = s.execute(text("SELECT user_id, nickname, remark FROM friends ORDER BY user_id ASC"))
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------- 消息


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_messages(
    s: Session,
    *,
    group_id: int | None,
    user_id: int | None,
    q: str | None,
    limit: int,
    before_id: int | None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if group_id is not None:
        clauses.append("group_id = :group_id")
        params["group_id"] = group_id
    if user_id is not None:
        clauses.append("user_id = :user_id")
        params["user_id"] = user_id
    if q:
        clauses.append("text LIKE :q ESCAPE '\\'")
        params["q"] = f"%{_escape_like(q)}%"
    if before_id is not None:
        clauses.append("id < :before_id")
        params["before_id"] = before_id
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT {MESSAGE_COLUMNS} FROM messages{where} ORDER BY time DESC, id DESC LIMIT :limit"
    return [dict(r._mapping) for r in s.execute(text(sql), params)]


def get_message(s: Session, msg_id: int) -> dict[str, Any] | None:
    row = s.execute(
        text(f"SELECT {MESSAGE_COLUMNS}, raw_json FROM messages WHERE msg_id = :msg_id ORDER BY id DESC LIMIT 1"),
        {"msg_id": msg_id},
    ).first()
    return dict(row._mapping) if row is not None else None


def count_messages_since(s: Session, since_ts: int) -> int:
    return int(
        s.execute(text("SELECT COUNT(*) FROM messages WHERE time >= :since"), {"since": since_ts}).scalar_one()
    )


# ---------------------------------------------------------------- 运行时设置


def get_settings_kv(s: Session) -> dict[str, str]:
    return {str(k): str(v) for k, v in s.execute(text("SELECT key, value FROM settings"))}


def set_settings_kv(s: Session, values: dict[str, str], now: str) -> None:
    rows = [{"key": k, "value": v, "updated_at": now} for k, v in values.items()]
    if not rows:
        return
    s.execute(
        text(
            "INSERT INTO settings (key, value, updated_at) VALUES (:key, :value, :updated_at) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        ),
        rows,
    )
