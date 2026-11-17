"""实时事件链路：events.parse → 保留期截止线 → gate.check → ingest.write（M2 在此之后接管线）。"""

from __future__ import annotations

import logging
from typing import Any

from . import accounts, gate, ingest, repo, runtime_config
from .db import Database
from .events import hub
from .models import COUNTER_MESSAGES_DROPPED, VIA_LIVE
from .napcat import events
from .napcat.events import as_int

log = logging.getLogger(__name__)


def handle_live_event(db: Database, frame: dict[str, Any]) -> str:
    """同步函数（在线程里跑）。返回 ignored / dropped / inserted / duplicate。

    多账号：事件里的 self_id 决定落哪个库；没有 self_id 时走作用域规则（最后登录号 → 主库归属账号）。
    touch 对并发认领 / 唯一约束冲突做了重查，不会因此丢事件。
    """
    msg = events.parse(frame)
    if msg is None:
        return "ignored"

    base = accounts.global_db(db)
    acc = accounts.touch(base, as_int(frame.get("self_id")))
    acc_id = acc.get("id")
    if acc_id is None:  # 只会发生在与"删账号"并发的极端时刻
        log.warning("事件找不到目标账号，丢弃 msg_id=%s self_id=%s", msg.msg_id, frame.get("self_id"))
        return "ignored"
    acc_id = int(acc_id)
    target = accounts.db_for(base, acc_id)

    token = accounts.bind_scope_account(acc_id)  # 本线程内后续推送自动带 account_id
    try:
        return _gate_and_write(target, msg, acc_id, acc.get("self_id"))
    finally:
        accounts.reset_scope_account(token)


def _gate_and_write(db: Database, msg: Any, account_id: int, self_id: int | None) -> str:
    # 早于保留期的消息（离线重放等）不入库，否则清理掉又会被拉回来。不计入"闸门丢弃"。
    if runtime_config.message_too_old(db, msg.time):
        log.info("早于保留期，跳过 account=%s type=%s msg_id=%s time=%s",
                 account_id, msg.message_type, msg.msg_id, msg.time)
        return "ignored"

    with db.session() as s:
        wl = repo.load_whitelist(s)

    decision = gate.check(msg, wl)
    if not decision.allowed:
        if decision.counted:
            db.incr_counter(COUNTER_MESSAGES_DROPPED)
        log.info(
            "闸门丢弃 account=%s type=%s msg_id=%s group=%s reason=%s",
            account_id, msg.message_type, msg.msg_id, msg.group_id, decision.reason,
        )
        return "dropped" if decision.counted else "ignored"

    inserted = ingest.write(db, msg, VIA_LIVE)
    if inserted:
        hub.publish({
            "type": "message.created",
            "account_id": account_id,
            "self_id": self_id,
            "msg_id": msg.msg_id,
            "group_id": msg.group_id,
            "user_id": msg.user_id,
            "message_type": msg.message_type,
            "sender_name": msg.sender_name,
            "text": (msg.text or "")[:200],
            "time": msg.time,
        })
    return "inserted" if inserted else "duplicate"
