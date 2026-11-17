"""Pydantic 请求/响应模型。字段名与前端调用保持一致。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------- 基础
class LoginStatus(BaseModel):
    logged_in: bool
    self_id: int | None = None
    nickname: str | None = None


class SelfCheckItem(BaseModel):
    name: str
    ok: bool
    detail: str


class SelfCheckOut(BaseModel):
    ok: bool
    checks: list[SelfCheckItem]


# ---------------------------------------------------------------- 同步
class SyncResult(BaseModel):
    groups: int
    members: int
    friends: int


class BackfillResult(BaseModel):
    groups: int
    scanned: int
    inserted: int


# ---------------------------------------------------------------- 群 / 成员 / 好友
class GroupOut(BaseModel):
    group_id: int
    group_name: str
    member_count: int
    whitelisted: bool
    mode: str | None = None
    whitelisted_member_ids: list[int] = Field(default_factory=list)


class MemberOut(BaseModel):
    user_id: int
    nickname: str
    card: str
    role: str
    whitelisted: bool


class FriendOut(BaseModel):
    user_id: int
    nickname: str
    remark: str
    whitelisted: bool


# ---------------------------------------------------------------- 白名单
class WhitelistGroupIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_id: int
    # 用 str 而不是 Literal：非法 mode 走业务校验返回 400 + 中文原因
    mode: str
    member_ids: list[int] | None = None


class WhitelistIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    groups: list[WhitelistGroupIn] | None = None
    users: list[int] | None = None


class WhitelistGroupOut(BaseModel):
    group_id: int
    mode: str
    member_ids: list[int]


class WhitelistOut(BaseModel):
    groups: list[WhitelistGroupOut]
    users: list[int]


# ---------------------------------------------------------------- 消息
class MessageOut(BaseModel):
    id: int
    msg_id: int
    group_id: int | None = None
    user_id: int
    message_type: str
    sender_name: str
    time: int
    text: str
    via: str
    ingested_at: str


class MessageDetail(MessageOut):
    raw: Any = None


# ---------------------------------------------------------------- 统计
class StatsOut(BaseModel):
    messages_total: int
    messages_today: int
    messages_dropped: int
    groups_cached: int
    whitelist_groups: int
    whitelist_users: int
    napcat_connected: bool


# ---------------------------------------------------------------- 设置
class SettingsOut(BaseModel):
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_api_key_set: bool
    backfill_enabled: bool
    backfill_count: int
    adapter_ws_url: str
    access_token_masked: str


class SettingsUpdate(BaseModel):
    """部分更新：只认这五个字段，未知字段忽略（不报 422）。"""

    model_config = ConfigDict(extra="ignore")

    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    backfill_enabled: bool | None = None
    backfill_count: int | None = Field(default=None, ge=1)
