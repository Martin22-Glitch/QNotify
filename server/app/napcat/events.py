"""OneBot v11 事件解析：message 事件 → IncomingMessage；其它事件返回 None。"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..models import MESSAGE_TYPE_GROUP, MESSAGE_TYPE_PRIVATE, IncomingMessage

log = logging.getLogger(__name__)


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def segments_to_text(message: Any, raw_message: Any = None) -> str:
    """text 段原样拼接；图片 → [图片]；其它段 → [文件:name]。"""
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return str(raw_message or "")
    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "image":
            parts.append("[图片]")
        else:
            name = data.get("name") or data.get("file") or seg_type or "未知"
            parts.append(f"[文件:{name}]")
    return "".join(parts)


def sender_name_of(sender: dict[str, Any]) -> str:
    return str(sender.get("card") or sender.get("nickname") or "")


def parse(frame: Any) -> IncomingMessage | None:
    if not isinstance(frame, dict) or frame.get("post_type") != "message":
        return None
    message_type = frame.get("message_type")
    if message_type not in (MESSAGE_TYPE_GROUP, MESSAGE_TYPE_PRIVATE):
        return None

    sender = frame.get("sender")
    if not isinstance(sender, dict):
        sender = {}

    msg_id = as_int(frame.get("message_id"))
    user_id = as_int(frame.get("user_id"))
    if user_id is None:
        user_id = as_int(sender.get("user_id"))
    if msg_id is None or user_id is None:
        log.debug("消息事件缺少 message_id/user_id，已忽略")
        return None

    group_id: int | None = None
    if message_type == MESSAGE_TYPE_GROUP:
        group_id = as_int(frame.get("group_id"))
        if group_id is None:
            log.debug("群消息缺少 group_id，已忽略 msg_id=%s", msg_id)
            return None

    ts = as_int(frame.get("time")) or int(time.time())
    return IncomingMessage(
        msg_id=msg_id,
        message_type=message_type,
        group_id=group_id,
        user_id=user_id,
        sender_name=sender_name_of(sender),
        time=ts,
        text=segments_to_text(frame.get("message"), frame.get("raw_message")),
        raw=frame,
    )


def parse_history(item: Any, group_id: int) -> IncomingMessage | None:
    """get_group_msg_history 返回的单条消息：补齐可能缺失的 post_type/message_type/group_id。"""
    if not isinstance(item, dict):
        return None
    frame = dict(item)
    frame.setdefault("post_type", "message")
    frame.setdefault("message_type", MESSAGE_TYPE_GROUP)
    if frame.get("group_id") is None:
        frame["group_id"] = group_id
    return parse(frame)
