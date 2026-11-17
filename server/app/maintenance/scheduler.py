"""定时备份：lifespan 里的一个 asyncio 任务，真正的 IO 走 to_thread。异常只记录。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import runtime_config
from ..db import Database
from . import backup, service

log = logging.getLogger(__name__)

STARTUP_GRACE_SECONDS = 120  # 启动后先让路给补采/建连；"距上次超过间隔"的补备份在这之后发生
TICK_SECONDS = 60


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.strptime(ts or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def last_backup(root: Any) -> dict[str, Any] | None:
    """最近一份非 preop 的有效备份。"""
    for b in backup.list_backups(root):
        if b["ok"] and b["reason"] != "preop":
            return b
    return None


def next_due(db: Database, settings: Any) -> datetime | None:
    cfg = runtime_config.load_backup(db)
    if not cfg.enabled:
        return None
    last = last_backup(backup.backup_root(settings, cfg.dir))
    t = _parse(last["created_at"]) if last else None
    return (t + timedelta(hours=cfg.interval_hours)) if t else datetime.now(UTC)


def run_if_due(db: Database, settings: Any) -> None:
    due = next_due(db, settings)
    if due is None or datetime.now(UTC) < due:
        return
    try:
        service.run_backup(db, settings, "scheduled")
    except backup.BackupBusy:
        pass


async def backup_loop(db: Database, settings: Any) -> None:
    await asyncio.sleep(STARTUP_GRACE_SECONDS)
    while True:
        try:
            await asyncio.to_thread(run_if_due, db, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("定时备份失败（不影响业务）")
        await asyncio.sleep(TICK_SECONDS)
