"""白名单闸门：纯判定函数，不碰数据库、不发网络。

判定顺序（顺序本身就是需求）：
1. 白名单群为空且私聊名单为空 → 丢弃
2. 群消息：群不在白名单 → 丢；all → 放行；members → 发送者在成员列表才放行
3. 私聊：发送者在私聊名单才放行
4. 其它类型 → 忽略（不计数）
丢弃计数由调用方（pipeline）根据 GateDecision.counted 执行。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    MESSAGE_TYPE_GROUP,
    MESSAGE_TYPE_PRIVATE,
    MODE_ALL,
    MODE_MEMBERS,
    IncomingMessage,
    WhitelistSnapshot,
)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str
    counted: bool = True


def check(msg: IncomingMessage, wl: WhitelistSnapshot) -> GateDecision:
    if wl.is_empty:
        return GateDecision(False, "whitelist_empty")

    if msg.message_type == MESSAGE_TYPE_GROUP:
        mode = wl.groups.get(msg.group_id) if msg.group_id is not None else None
        if mode is None:
            return GateDecision(False, "group_not_whitelisted")
        if mode == MODE_ALL:
            return GateDecision(True, "group_all")
        if mode == MODE_MEMBERS:
            if msg.user_id in wl.members.get(msg.group_id, frozenset()):
                return GateDecision(True, "group_member")
            return GateDecision(False, "sender_not_whitelisted")
        return GateDecision(False, "unknown_mode")

    if msg.message_type == MESSAGE_TYPE_PRIVATE:
        if msg.user_id in wl.users:
            return GateDecision(True, "private_user")
        return GateDecision(False, "private_not_whitelisted")

    return GateDecision(False, "ignored_type", counted=False)
