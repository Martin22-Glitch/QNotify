"""/api/maintenance/*。鉴权由 /api 路由器统一完成。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from .. import __version__, runtime_config
from ..db import SCHEMA_VERSION
from . import backup, mlog, restore, scheduler, service

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class BackupIn(BaseModel):
    reason: str = "manual"


class RestoreIn(BaseModel):
    name: str
    confirm: str


def _ctx(request: Request) -> tuple[Any, Any]:
    from .. import accounts

    ctx = request.app.state.ctx
    return accounts.global_db(ctx.db), ctx.settings


def _bytes(p: Path) -> int:
    return sum(q.stat().st_size for q in (p, Path(f"{p}-wal"), Path(f"{p}-shm")) if q.exists())


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    from .. import accounts

    db, settings = _ctx(request)
    cfg = runtime_config.load_backup(db)
    root = backup.backup_root(settings, cfg.dir)
    with db.session() as s:
        free = int(s.execute(text("PRAGMA freelist_count")).scalar() or 0)
        page = int(s.execute(text("PRAGMA page_size")).scalar() or 0)
    accs = []
    for r in accounts.list_all(db):
        p = accounts.db_path_of(db, r["id"])
        accs.append({"id": r["id"], "self_id": r.get("self_id"), "nickname": r.get("nickname"),
                     "bytes": _bytes(p) if p.exists() else 0})
    items = backup.list_backups(root)
    last = items[0] if items else None
    due = scheduler.next_due(db, settings)
    return {
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "primary_confirm_word": restore.confirm_words(db)[0],
        "db": {"path": str(db.path), "bytes": _bytes(db.path), "reclaimable_bytes": free * page},
        "accounts": accs,
        "last_backup": ({k: last[k] for k in ("name", "created_at", "ok", "reason")} if last else None),
        "next_backup_at": due.strftime("%Y-%m-%dT%H:%M:%SZ") if due else None,
        "backups": {"dir": str(root), "count": len(items), "bytes": sum(b["bytes"] for b in items),
                    "same_disk": backup.same_disk(settings, root),
                    "contains_secrets": True},
        "restore_pending": restore.pending(settings),
    }


@router.get("/backups")
def list_backups(request: Request) -> list[dict[str, Any]]:
    db, settings = _ctx(request)
    return backup.list_backups(backup.backup_root(settings, runtime_config.load_backup(db).dir))


@router.post("/backup")
def make_backup(request: Request, body: BackupIn | None = None) -> dict[str, Any]:
    db, settings = _ctx(request)
    reason = (body.reason if body else "manual") or "manual"
    if reason not in ("manual", "preop", "preclean"):
        raise HTTPException(400, "reason 只能是 manual / preop / preclean")
    try:
        res = service.run_backup(db, settings, reason)
    except backup.BackupBusy:
        raise HTTPException(409, "备份正在进行") from None
    except Exception as exc:
        raise HTTPException(500, f"备份失败：{type(exc).__name__}") from exc
    return {"name": res["name"], "ok": True, "bytes": res["bytes"], "files": res["files"]}


@router.post("/restore")
def queue_restore(request: Request, body: RestoreIn) -> dict[str, Any]:
    db, settings = _ctx(request)
    root = backup.backup_root(settings, runtime_config.load_backup(db).dir)
    if backup.LOCK.locked():
        raise HTTPException(409, "备份正在进行")
    try:
        return restore.queue(settings, db, root, body.name, body.confirm)
    except restore.ConfirmMismatch as exc:
        raise HTTPException(400, str(exc)) from exc
    except backup.BackupNotFound:
        raise HTTPException(404, "备份不存在") from None
    except (backup.BackupTooNew, backup.BackupInvalid) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/log")
def get_log(request: Request, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    db, _ = _ctx(request)
    return mlog.recent(db, limit)
