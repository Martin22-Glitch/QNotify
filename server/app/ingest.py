"""入库：IncomingMessage → messages 行。去重完全依赖 UNIQUE(message_type, msg_id)。"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import Database, utc_now_iso
from .models import IncomingMessage

log = logging.getLogger(__name__)

_INSERT_MESSAGE = text(
    "INSERT INTO messages "
    "(msg_id, group_id, user_id, message_type, sender_name, time, text, raw_json, via, ingested_at) "
    "VALUES (:msg_id, :group_id, :user_id, :message_type, :sender_name, :time, :text, :raw_json, :via, :ingested_at)"
)


def build_row(msg: IncomingMessage, via: str) -> dict[str, Any]:
    return {
        "msg_id": msg.msg_id,
        "group_id": msg.group_id,
        "user_id": msg.user_id,
        "message_type": msg.message_type,
        "sender_name": msg.sender_name,
        "time": msg.time,
        "text": msg.text,
        "raw_json": json.dumps(msg.raw, ensure_ascii=False, default=str),
        "via": via,
        "ingested_at": utc_now_iso(),
    }


def write(db: Database, msg: IncomingMessage, via: str) -> bool:
    """直接 INSERT；撞 UNIQUE 约束即回滚跳过。返回是否真正新增。

     兜底：早于保留期的消息不写（调用方已先判过，这里防止以后新增入口漏判）。
    """
    from . import runtime_config

    if runtime_config.message_too_old(db, msg.time):
        log.debug("早于保留期，不入库 type=%s msg_id=%s time=%s", msg.message_type, msg.msg_id, msg.time)
        return False
    row = build_row(msg, via)
    with db.write_lock:
        with db.session() as s:
            try:
                s.execute(_INSERT_MESSAGE, row)
                s.commit()
            except IntegrityError:
                s.rollback()
                log.debug("重复消息已跳过 type=%s msg_id=%s", msg.message_type, msg.msg_id)
                return False
    log.info("入库 type=%s msg_id=%s group=%s via=%s", msg.message_type, msg.msg_id, msg.group_id, via)
    if log.isEnabledFor(logging.DEBUG):
        log.debug("正文预览 msg_id=%s：%s", msg.msg_id, msg.text[:30])
    return True
