"""备份的组合入口：读设置 → 备份（含保留清理）→ 写维护记录。"""

from __future__ import annotations

from typing import Any

from .. import runtime_config
from ..db import Database
from ..models import MAINT_BACKUP
from . import backup, mlog


def run_backup(db: Database, settings: Any, reason: str, *, blocking: bool = False) -> dict[str, Any]:
    cfg = runtime_config.load_backup(db)
    root = backup.backup_root(settings, cfg.dir)
    try:
        res = backup.create_backup(
            settings, reason, root=root, keep=cfg.keep, keep_preop=cfg.keep_preop, blocking=blocking
        )
    except backup.BackupBusy:
        raise
    except Exception as exc:
        mlog.write(db, MAINT_BACKUP, {"reason": reason, "error": type(exc).__name__, "msg": str(exc)[:200]}, False)
        raise
    mlog.write(db, MAINT_BACKUP, {"reason": reason, "name": res["name"], "bytes": res["bytes"],
                                  "files": res["files"], "pruned": res["pruned"]}, True)
    return res
