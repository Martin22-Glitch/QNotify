"""整套恢复：运行期 queue() 只写标记；启动期 apply_pending() 在 Database.open 之前执行替换。"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db import Database, migrate_file, release_open_databases, utc_now_iso
from . import backup

log = logging.getLogger(__name__)

MARKER = "restore.pending"
RESULT = "restore.result.json"
SIDE = ("-wal", "-shm", "-journal")


class ConfirmMismatch(ValueError):
    pass


def marker_path(settings: Any) -> Path:
    return backup.data_dir(settings) / MARKER


def pending(settings: Any) -> dict[str, Any] | None:
    try:
        return json.loads(marker_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def confirm_words(db: Database) -> tuple[str, list[str]]:
    """主库归属账号的 self_id（首选）与 nickname 都可作确认词。"""
    from .. import accounts

    bid = accounts.base_account_id(db)
    row = accounts.get(db, bid) if bid is not None else None
    words: list[str] = []
    if row:
        if row.get("self_id"):
            words.append(str(row["self_id"]))
        if (row.get("nickname") or "").strip():
            words.append(str(row["nickname"]).strip())
    if not words:
        words.append(f"account-{bid}")
    return words[0], words


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def queue(settings: Any, db: Database, root: Path, name: str, confirm: str) -> dict[str, Any]:
    _, words = confirm_words(db)
    if (confirm or "").strip() not in words:
        raise ConfirmMismatch("确认词不正确")
    backup.verify(settings, root, name)  # NotFound / Invalid / TooNew 由路由映射
    _atomic_write(marker_path(settings), {"backup": name, "requested_at": utc_now_iso()})
    log.info("已排队恢复：%s（重启后执行）", name)
    return {"ok": True, "pending": True, "backup": name, "restart_required": True}


def _strip_sidecars(db_file: Path) -> None:
    for s in SIDE:
        p = Path(str(db_file) + s)
        if p.exists():
            p.unlink()


def _result(settings: Any, data: dict[str, Any]) -> dict[str, Any]:
    data.setdefault("at", utc_now_iso())
    try:
        _atomic_write(backup.data_dir(settings) / RESULT, data)
    except OSError:
        log.warning("写恢复结果文件失败", exc_info=True)
    (log.info if data.get("ok") else log.error)("恢复结果：ok=%s %s", data.get("ok"), data.get("error", ""))
    return data


def _retire_marker(mp: Path, suffix: str) -> None:
    try:
        os.replace(mp, mp.with_name(MARKER + suffix))
    except OSError:
        log.warning("无法改名标记文件 %s", mp, exc_info=True)


def apply_pending(settings: Any) -> dict[str, Any] | None:
    """启动阶段调用。返回 None 表示无事可做；否则返回结果（同时落 restore.result.json，开库后记维护日志）。"""
    mp = marker_path(settings)
    if not mp.exists():
        return None
    data = backup.data_dir(settings)
    try:
        name = str(json.loads(mp.read_text(encoding="utf-8"))["backup"])
    except (OSError, ValueError, KeyError, TypeError):
        _retire_marker(mp, ".bad")
        return _result(settings, {"ok": False, "stage": "marker", "error": "标记文件不可读"})

    with backup.LOCK:
        release_open_databases(data)  # 正常启动时为空；防运行中误调
        root = backup.backup_root(settings)

        # 1. 重新校验（排队后备份可能被动过）。不可恢复的错误：退役标记，避免每次启动都失败
        try:
            folder, manifest = backup.verify(settings, root, name)
        except (backup.BackupNotFound, backup.BackupInvalid, backup.BackupTooNew) as exc:
            _retire_marker(mp, ".failed")
            return _result(settings, {"ok": False, "stage": "verify", "backup": name, "error": str(exc)})

        # 2. 现状 preop 快照。失败 → 中止、保留标记（下次启动重试）
        try:
            pre = backup.create_locked(settings, "preop", root=root)
            backup.prune(root, None, int(backup.read_setting_raw(settings.db_file, "backup.keep_preop") or 3))
        except Exception as exc:
            return _result(settings, {"ok": False, "stage": "preop", "backup": name, "error": str(exc)[:200],
                                      "marker_kept": True})

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        replaced = data / f"replaced-{stamp}"
        moved: list[tuple[Path, Path]] = []
        placed: list[Path] = []
        try:
            # 3. 现状整体挪走（主库 + 旁文件 + accounts/ 整个目录）
            replaced.mkdir(parents=True)
            main = Path(settings.db_file)
            for p in [main, *(Path(str(main) + s) for s in SIDE), data / "accounts"]:
                if p.exists():
                    dst = replaced / p.name
                    os.replace(p, dst)
                    moved.append((p, dst))
            # 4. 备份文件落位（先拷成 .restoring，再原子改名）；5. 清掉任何旁文件
            for f in manifest["files"]:
                target = backup.target_for(settings, f["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".restoring")
                shutil.copyfile(folder / f["path"], tmp)
                os.replace(tmp, target)
                placed.append(target)
                _strip_sidecars(target)
            # 6. 迁移到当前版本（低版本备份补齐；同版本无操作）
            for t in placed:
                migrate_file(t)
                _strip_sidecars(t)
        except Exception as exc:
            # 回滚：删新放的，挪回旧的。回滚后退役标记，避免启动循环
            for t in placed:
                for p in (t, *(Path(str(t) + s) for s in SIDE), t.with_name(t.name + ".restoring")):
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
            rollback_ok = True
            if (data / "accounts").exists() and any(src.name == "accounts" for src, _ in moved):
                shutil.rmtree(data / "accounts", ignore_errors=True)
            for src, dst in reversed(moved):
                try:
                    os.replace(dst, src)
                except OSError:
                    rollback_ok = False
                    log.critical("回滚失败：%s 仍在 %s，需人工处理", src, dst)
            _retire_marker(mp, ".failed")
            return _result(settings, {"ok": False, "stage": "replace", "backup": name, "preop": pre["name"],
                                      "error": str(exc)[:200], "rollback_ok": rollback_ok,
                                      "replaced_dir": str(replaced)})

        # 7. 删标记（最后一步：之前任何崩溃都会在下次启动重来，且有 preop 兜底）
        try:
            mp.unlink()
        except OSError:
            log.error("恢复已完成但标记删除失败：%s（下次启动会再次恢复同一备份，结果幂等）", mp)
        return _result(settings, {"ok": True, "backup": name, "preop": pre["name"],
                                  "schema_version_from": manifest["schema_version"],
                                  "files": [f["path"] for f in manifest["files"]], "replaced_dir": str(replaced)})


def consume_result(settings: Any, db: Database) -> None:
    """开库后把上次恢复结果写进 maintenance_log，然后删结果文件。"""
    from ..models import MAINT_RESTORE
    from . import mlog

    p = backup.data_dir(settings) / RESULT
    if not p.exists():
        return
    try:
        res = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        res = {"ok": False, "error": "结果文件不可读"}
    mlog.write(db, MAINT_RESTORE, res, bool(res.get("ok")))
    try:
        p.unlink()
    except OSError:
        pass
