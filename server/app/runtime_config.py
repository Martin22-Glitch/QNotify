"""运行时配置：settings 表覆盖 env（仅限五个可改字段），以及 GET 时的掩码视图。

通用资产：load / apply_update 入口都先换成主库，任何作用域下读写的是同一份。
新增 backup.* 设置（同一张 settings 表，键带前缀；不进 SETTING_KEYS，/api/settings 不变）。
新增 retention.* 设置 + 保留期截止线（实时入库/补采/清理共用一个口径）。
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from . import repo
from .config import Settings
from .db import Database, utc_now_iso
from .models import (
    BACKUP_SETTING_DEFAULTS,
    RETENTION_INT_RANGES,
    RETENTION_SETTING_DEFAULTS,
    RETENTION_SETTING_KEYS,
    SECRET_MASK,
    SETTING_KEYS,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    backfill_enabled: bool
    backfill_count: int


@dataclass(frozen=True, slots=True)
class BackupConfig:
    enabled: bool
    interval_hours: int
    keep: int
    keep_preop: int
    dir: str


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    overdue_days: int
    content_days: int


# 取值范围（含端点）。超出范围的存量值按默认处理，写入时直接拒绝。
BACKUP_INT_RANGES: dict[str, tuple[int, int]] = {
    "backup.interval_hours": (1, 24 * 30),
    "backup.keep": (1, 1000),
    "backup.keep_preop": (1, 100),
}

_RETENTION_CACHE_ATTR = "_qn_retention_cache"


def _global(db: Database) -> Database:
    """通用资产（模型档案/系统设置/账本）只在主库，不跟账号走。"""
    from .accounts import global_db

    return global_db(db)


def _parse_bool(value: str, default: bool) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _parse_int(value: str, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


def load(db: Database, settings: Settings) -> RuntimeConfig:
    db = _global(db)
    with db.session() as s:
        kv = repo.get_settings_kv(s)
    return RuntimeConfig(
        llm_base_url=kv.get("llm_base_url", settings.llm_base_url),
        llm_model=kv.get("llm_model", settings.llm_model),
        llm_api_key=kv.get("llm_api_key", settings.llm_api_key),
        backfill_enabled=(
            _parse_bool(kv["backfill_enabled"], settings.backfill_enabled)
            if "backfill_enabled" in kv
            else settings.backfill_enabled
        ),
        backfill_count=(
            _parse_int(kv["backfill_count"], settings.backfill_count)
            if "backfill_count" in kv
            else settings.backfill_count
        ),
    )


def apply_update(db: Database, changes: dict[str, Any]) -> None:
    """部分更新。None 视为未传；llm_api_key="***" 不改，"" 清除。写入恒落主库。"""
    db = _global(db)
    rows: dict[str, str] = {}
    for key, value in changes.items():
        if key not in SETTING_KEYS or value is None:
            continue
        if key == "llm_api_key" and value == SECRET_MASK:
            continue
        if key == "backfill_enabled":
            rows[key] = "true" if bool(value) else "false"
        elif key == "backfill_count":
            rows[key] = str(int(value))
        else:
            rows[key] = str(value)
    if not rows:
        return
    with db.write() as s:
        repo.set_settings_kv(s, rows, utc_now_iso())


# ---------------------------------------------------------------- 通用 KV（带前缀的扩展键）


def _upsert_raw(db: Database, rows: dict[str, str]) -> None:
    now = utc_now_iso()
    with db.write() as s:
        for key, value in rows.items():
            s.execute(
                text(
                    "INSERT INTO settings (key, value, updated_at) VALUES (:k, :v, :t) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
                ),
                {"k": key, "v": value, "t": now},
            )


def get_raw(db: Database, key: str) -> str | None:
    """读主库 settings 表的一个原始值（不经 repo 的 SETTING_KEYS 过滤）。"""
    db = _global(db)
    with db.session() as s:
        row = s.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": key}).first()
    return None if row is None else str(row[0])


def set_raw(db: Database, key: str, value: str) -> None:
    _upsert_raw(_global(db), {key: value})


# ---------------------------------------------------------------- backup.*


def _backup_int(kv: dict[str, str], key: str) -> int:
    default = int(BACKUP_SETTING_DEFAULTS[key])
    n = _parse_int(kv.get(key, str(default)), default)
    lo, hi = BACKUP_INT_RANGES[key]
    return n if lo <= n <= hi else default


def load_backup(db: Database) -> BackupConfig:
    """读 backup.* 设置。直接查 settings 表（不经 repo，避免它按 SETTING_KEYS 过滤）。"""
    db = _global(db)
    with db.session() as s:
        rows = s.execute(text("SELECT key, value FROM settings WHERE key LIKE 'backup.%'")).all()
    kv = {str(r[0]): str(r[1]) for r in rows}
    return BackupConfig(
        enabled=_parse_bool(kv.get("backup.enabled", BACKUP_SETTING_DEFAULTS["backup.enabled"]), True),
        interval_hours=_backup_int(kv, "backup.interval_hours"),
        keep=_backup_int(kv, "backup.keep"),
        keep_preop=_backup_int(kv, "backup.keep_preop"),
        dir=kv.get("backup.dir", "").strip(),
    )




# ---------------------------------------------------------------- retention.*


def _retention_int(kv: dict[str, str], key: str) -> int:
    default = int(RETENTION_SETTING_DEFAULTS[key])
    n = _parse_int(kv.get(key, str(default)), default)
    lo, hi = RETENTION_INT_RANGES[key]
    return n if lo <= n <= hi else default


def default_retention() -> RetentionConfig:
    return RetentionConfig(
        overdue_days=int(RETENTION_SETTING_DEFAULTS["retention.overdue_days"]),
        content_days=int(RETENTION_SETTING_DEFAULTS["retention.content_days"]),
    )


def load_retention(db: Database, *, max_age: float = 0.0) -> RetentionConfig:
    """读 retention.*。max_age>0 时允许用不超过该秒数的缓存（实时入库热路径用）；写入会清缓存。"""
    db = _global(db)
    if max_age > 0:
        hit = getattr(db, _RETENTION_CACHE_ATTR, None)
        if hit is not None and time.monotonic() - hit[0] < max_age:
            return hit[1]
    with db.session() as s:
        rows = s.execute(text("SELECT key, value FROM settings WHERE key LIKE 'retention.%'")).all()
    kv = {str(r[0]): str(r[1]) for r in rows}
    cfg = RetentionConfig(
        overdue_days=_retention_int(kv, "retention.overdue_days"),
        content_days=_retention_int(kv, "retention.content_days"),
    )
    with contextlib.suppress(Exception):  # Database 若禁止加属性，就不缓存
        setattr(db, _RETENTION_CACHE_ATTR, (time.monotonic(), cfg))
    return cfg


def apply_retention_update(db: Database, changes: dict[str, Any]) -> None:
    """部分更新 retention.*。None 视为未传；非整数或越界抛 ValueError（不写任何一项）。"""
    db = _global(db)
    rows: dict[str, str] = {}
    for key, value in changes.items():
        # GET 返回的是短键（overdue_days / content_days），PUT 也接受短键，这里统一补前缀
        if key in ("overdue_days", "content_days"):
            key = "retention." + key
        if key not in RETENTION_SETTING_KEYS or value is None:
            continue
        if isinstance(value, bool):
            raise ValueError(f"{key} 必须是整数")
        try:
            n = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{key} 必须是整数") from None
        lo, hi = RETENTION_INT_RANGES[key]
        if not lo <= n <= hi:
            raise ValueError(f"{key} 超出范围 {lo}..{hi}")
        rows[key] = str(n)
    if not rows:
        return
    _upsert_raw(db, rows)
    with contextlib.suppress(Exception):
        setattr(db, _RETENTION_CACHE_ATTR, None)


def content_cutoff_epoch(db: Database, *, max_age: float = 10.0) -> int:
    """保留期截止线（Unix 秒）：time 早于它的消息不入库、会被清理。读设置失败时按默认天数。"""
    try:
        days = load_retention(db, max_age=max_age).content_days
    except Exception as exc:
        log.warning("读取保留期失败，按默认值：%s", type(exc).__name__)
        days = default_retention().content_days
    return int(time.time()) - days * 86400


def message_too_old(db: Database, ts: Any, *, max_age: float = 10.0) -> bool:
    """消息时间早于保留期 → True。时间缺失/非法（<=0）一律视为"不知道"，不拦。"""
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return False
    if t <= 0:
        return False
    return t < content_cutoff_epoch(db, max_age=max_age)


def mask_token(token: str) -> str:
    if len(token) >= 8:
        return token[:4] + SECRET_MASK
    return SECRET_MASK


def public_view(cfg: RuntimeConfig, settings: Settings) -> dict[str, Any]:
    has_key = bool(cfg.llm_api_key)
    return {
        "llm_base_url": cfg.llm_base_url,
        "llm_model": cfg.llm_model,
        "llm_api_key": SECRET_MASK if has_key else "",
        "llm_api_key_set": has_key,
        "backfill_enabled": cfg.backfill_enabled,
        "backfill_count": cfg.backfill_count,
        "adapter_ws_url": settings.adapter_ws_url,
        "access_token_masked": mask_token(settings.access_token),
    }
