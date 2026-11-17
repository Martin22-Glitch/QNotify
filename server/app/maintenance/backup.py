"""一致性备份：逐库 VACUUM INTO → quick_check → sha256 → manifest → 原子改名。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..db import SCHEMA_VERSION

log = logging.getLogger(__name__)

FORMAT = 1
MAIN_REL = "qnotify.db"
REASONS = frozenset({"manual", "scheduled", "preop", "preclean"})
NAME_RE = re.compile(r"^backup-(\d{8}-\d{6})-(manual|scheduled|preop|preclean)(?:-\d+)?$")
_ACC_DIR_RE = re.compile(r"^acc-(\d+)$")
_ACC_REL_RE = re.compile(r"^accounts/acc-(\d+)\.db$")
TMP_STALE_SECONDS = 6 * 3600

# 进程内唯一的备份/恢复互斥：手动、定时、启动恢复共用。拿不到 → 409。
LOCK = threading.Lock()


class BackupBusy(RuntimeError):
    pass


class BackupNotFound(LookupError):
    pass


class BackupInvalid(ValueError):
    pass


class BackupTooNew(ValueError):
    pass


# ---------------------------------------------------------------- 路径


def data_dir(settings: Any) -> Path:
    return Path(settings.db_file).parent


def read_setting_raw(db_file: Path, key: str) -> str | None:
    """不经 Database 读主库 settings（启动恢复时库还没打开）。任何错误返回 None。"""
    if not Path(db_file).exists():
        return None
    try:
        conn = sqlite3.connect(f"{Path(db_file).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def backup_root(settings: Any, configured: str | None = None) -> Path:
    if configured is None:
        configured = (read_setting_raw(settings.db_file, "backup.dir") or "").strip()
    if not configured:
        return data_dir(settings) / "backups"
    p = Path(configured)
    return p if p.is_absolute() else data_dir(settings) / p


def same_disk(settings: Any, root: Path) -> bool:
    """备份目录与数据是否同盘（界面据此提示"不防硬盘损坏"）。"""
    try:
        probe = root if root.exists() else root.parent
        return os.stat(probe).st_dev == os.stat(data_dir(settings)).st_dev
    except OSError:
        return True


def sources(settings: Any) -> list[tuple[str, Path]]:
    """待备份的库：主库 + data/accounts/acc-<id>/qnotify.db。按磁盘枚举，不依赖已打开的 Database。"""
    out: list[tuple[str, Path]] = []
    main = Path(settings.db_file)
    if main.exists():
        out.append((MAIN_REL, main))
    acc_root = data_dir(settings) / "accounts"
    if acc_root.is_dir():
        for d in sorted(acc_root.iterdir()):
            m = _ACC_DIR_RE.match(d.name)
            f = d / "qnotify.db"
            if m and f.is_file():
                out.append((f"accounts/acc-{m.group(1)}.db", f))
    return out


def target_for(settings: Any, rel: str) -> Path:
    """manifest 内相对路径 → 恢复时的落盘位置。未知形态一律拒绝（防路径穿越）。"""
    if rel == MAIN_REL:
        return Path(settings.db_file)
    m = _ACC_REL_RE.match(rel)
    if not m:
        raise BackupInvalid(f"manifest 中有无法识别的文件：{rel}")
    return data_dir(settings) / "accounts" / f"acc-{m.group(1)}" / "qnotify.db"


# ---------------------------------------------------------------- 单文件操作


def _snapshot(src: Path, dst: Path) -> None:
    conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=rw", uri=True, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM INTO ?", (str(dst),))
    finally:
        conn.close()


def quick_check(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _accounts_of(main_snapshot: Path) -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(f"{main_snapshot.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [{"id": r["id"], "self_id": r["self_id"] if "self_id" in r.keys() else None} for r in rows]


# ---------------------------------------------------------------- 创建


def create_backup(
    settings: Any,
    reason: str,
    *,
    root: Path | None = None,
    keep: int | None = None,
    keep_preop: int | None = None,
    blocking: bool = False,
) -> dict[str, Any]:
    if reason not in REASONS:
        raise ValueError(f"未知备份原因：{reason}")
    if not LOCK.acquire(blocking=blocking):
        raise BackupBusy("备份正在进行")
    try:
        return create_locked(settings, reason, root=root, keep=keep, keep_preop=keep_preop)
    finally:
        LOCK.release()


def create_locked(
    settings: Any,
    reason: str,
    *,
    root: Path | None = None,
    keep: int | None = None,
    keep_preop: int | None = None,
) -> dict[str, Any]:
    """调用方必须已持有 LOCK。"""
    root = root or backup_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    name = f"backup-{stamp}-{reason}"
    n = 2
    while (root / name).exists():
        name = f"backup-{stamp}-{reason}-{n}"
        n += 1
    tmp = root / f".tmp-{stamp}-{reason}-{os.getpid()}"
    srcs = sources(settings)
    if not srcs:
        raise BackupInvalid("没有可备份的库文件")
    try:
        tmp.mkdir(parents=True)
        # 第一轮只做快照：各库之间的时间差 = 前面几个库的 VACUUM 耗时之和，不夹杂哈希/校验
        for rel, src in srcs:
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _snapshot(src, dst)
        files: list[dict[str, Any]] = []
        for rel, _src in srcs:
            dst = tmp / rel
            if not quick_check(dst):
                raise BackupInvalid(f"快照 quick_check 未通过：{rel}")
            files.append({"path": rel, "bytes": dst.stat().st_size, "sha256": sha256_of(dst)})
        manifest = {
            "format": FORMAT,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": reason,
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "accounts": _accounts_of(tmp / MAIN_REL) if (tmp / MAIN_REL).exists() else [],
            "files": files,
        }
        mf = tmp / "manifest.json"
        with open(mf, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, root / name)  # 同目录改名：要么出现完整备份，要么什么都没有
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log.info("备份完成：%s（%d 个文件）", name, len(files))
    removed = prune(root, keep, keep_preop) if keep else []
    return {
        "name": name,
        "ok": True,
        "bytes": sum(f["bytes"] for f in files),
        "files": [f["path"] for f in files],
        "created_at": manifest["created_at"],
        "reason": reason,
        "pruned": removed,
    }


# ---------------------------------------------------------------- 列表 / 保留


def _names(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir() and NAME_RE.match(p.name)), reverse=True)


def prune(root: Path, keep: int | None, keep_preop: int | None) -> list[str]:
    """普通备份保留最近 keep 份，preop 另外保留最近 keep_preop 份。只删符合命名规则的目录。"""
    removed: list[str] = []
    normal = [n for n in _names(root) if NAME_RE.match(n).group(2) != "preop"]  # type: ignore[union-attr]
    preop = [n for n in _names(root) if NAME_RE.match(n).group(2) == "preop"]  # type: ignore[union-attr]
    for group, limit in ((normal, keep), (preop, keep_preop)):
        if not limit:
            continue
        for n in group[limit:]:
            try:
                shutil.rmtree(root / n)
                removed.append(n)
            except OSError:
                log.warning("清理旧备份失败：%s", n, exc_info=True)
    for p in root.glob(".tmp-*"):
        try:
            if p.is_dir() and time.time() - p.stat().st_mtime > TMP_STALE_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    return removed


def read_manifest(folder: Path) -> dict[str, Any]:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupInvalid("manifest 不可读") from exc


def list_backups(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in _names(root):
        folder = root / name
        try:
            m = read_manifest(folder)
            out.append({
                "name": name,
                "created_at": m.get("created_at"),
                "reason": m.get("reason"),
                "bytes": sum(int(f.get("bytes", 0)) for f in m.get("files", [])),
                "accounts": len(m.get("accounts", [])),
                "schema_version": m.get("schema_version"),
                "ok": True,
            })
        except BackupInvalid:
            out.append({"name": name, "created_at": None, "reason": NAME_RE.match(name).group(2),  # type: ignore[union-attr]
                        "bytes": 0, "accounts": 0, "schema_version": None, "ok": False})
    return out


def verify(settings: Any, root: Path, name: str) -> tuple[Path, dict[str, Any]]:
    """完整校验：命名、存在、manifest、版本、路径合法、sha256、quick_check。"""
    if not NAME_RE.match(name or ""):
        raise BackupNotFound(name)
    folder = root / name
    if not folder.is_dir():
        raise BackupNotFound(name)
    m = read_manifest(folder)
    if m.get("format") != FORMAT:
        raise BackupInvalid(f"不支持的备份格式：{m.get('format')}")
    sv = m.get("schema_version")
    if not isinstance(sv, int):
        raise BackupInvalid("manifest 缺少 schema_version")
    if sv > SCHEMA_VERSION:
        raise BackupTooNew(f"备份来自更新的版本（schema {sv} > 当前 {SCHEMA_VERSION}）")
    files = m.get("files") or []
    if MAIN_REL not in {f.get("path") for f in files}:
        raise BackupInvalid("备份缺少主库")
    for f in files:
        rel = str(f.get("path"))
        target_for(settings, rel)  # 形态校验
        p = folder / rel
        if not p.is_file():
            raise BackupInvalid(f"文件缺失：{rel}")
        if sha256_of(p) != f.get("sha256"):
            raise BackupInvalid(f"哈希校验失败：{rel}")
        if not quick_check(p):
            raise BackupInvalid(f"quick_check 未通过：{rel}")
    return folder, m
