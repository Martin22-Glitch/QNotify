"""多模型档案：用户可以保存多套模型配置，在设置页里切换启用。

为什么需要：以前只存一套（provider/base_url/key/model），换模型就得覆盖旧的，
想留两套（比如"便宜的做判定、强一点的做对话"或"公司代理 / 家里直连"）就做不到。

设计：
- 表 `llm_profiles`，每行一套配置；`active=1` 表示当前生效，任何时刻最多一条
- 密钥只存不读：对外接口一律返回 `api_key_set` 布尔，绝不返回明文
- 删除当前生效的档案后，自动启用剩下最新的一条（没有就回到历史单配置逻辑）
- 历史数据做一次性迁移：老的单配置（settings 表 + profile 的 setting.llm_provider）
  搬成一条名为「默认」的档案并置为生效
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("app.llm_profiles")

PROVIDERS = ("openai", "anthropic")
NAME_MAX = 40
BASE_URL_MAX = 300
MODEL_MAX = 120
KEY_MAX = 400

DDL = """
CREATE TABLE IF NOT EXISTS llm_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'openai',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_test_at TEXT,
    last_test_ok INTEGER,
    last_test_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_llm_profiles_active ON llm_profiles (active);
"""

_COLS = "id, name, provider, base_url, model, api_key, active, created_at, updated_at, last_test_at, last_test_ok, last_test_error"


def _global(db):
    """通用资产（模型档案/系统设置/账本）只在主库，不跟账号走。"""
    from .accounts import global_db

    return global_db(db)

def _rows(db: Any, sql_text: str, **params: Any) -> list[dict[str, Any]]:
    from app.m2 import _rows as rows

    return rows(db, sql_text, **params)


def _exec(s: Any, sql_text: str, **params: Any) -> int:
    from app.m2 import _exec as ex

    return ex(s, sql_text, **params)


def _now(db: Any) -> str:
    from app.m2 import now_iso

    return now_iso(db)


def _mask(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row[k] for k in row if k != "api_key"}
    out["api_key_set"] = bool((row.get("api_key") or "").strip())
    out["active"] = bool(row.get("active"))
    if row.get("last_test_ok") is not None:
        out["last_test_ok"] = bool(row["last_test_ok"])
    return out


def list_all(db: Any) -> list[dict[str, Any]]:
    db = _global(db)
    return [_mask(r) for r in _rows(db, f"SELECT {_COLS} FROM llm_profiles ORDER BY active DESC, id")]


def active(db: Any) -> dict[str, Any] | None:
    """当前生效的档案（含明文 key，只在服务端内部用）。"""
    db = _global(db)
    rows = _rows(db, f"SELECT {_COLS} FROM llm_profiles WHERE active=1 ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def get(db: Any, pid: int) -> dict[str, Any] | None:
    db = _global(db)
    rows = _rows(db, f"SELECT {_COLS} FROM llm_profiles WHERE id=:i", i=pid)
    return rows[0] if rows else None


def validate(data: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """校验并规整入参；partial=True 时只校验传了的字段（用于 PATCH）。"""
    from app.m2 import _bad

    out: dict[str, Any] = {}
    if not partial or data.get("provider") is not None:
        provider = str(data.get("provider") or "openai").strip().lower()
        if provider not in PROVIDERS:
            raise _bad("保存失败：协议只能是 openai 或 anthropic")
        out["provider"] = provider
    if not partial or data.get("name") is not None:
        name = str(data.get("name") or "").strip()[:NAME_MAX]
        out["name"] = name
    for field in ("base_url", "model", "api_key"):
        if not partial or data.get(field) is not None:
            value = str(data.get(field) or "").strip()
        else:
            continue
        if field == "base_url" and len(value) > BASE_URL_MAX:
            raise _bad("保存失败：接口地址太长")
        if field == "model" and len(value) > MODEL_MAX:
            raise _bad("保存失败：模型名太长")
        if field == "api_key" and len(value) > KEY_MAX:
            raise _bad("保存失败：密钥太长")
        out[field] = value
    if not partial:
        if not out.get("model"):
            raise _bad("保存失败：模型名不能为空")
        out.setdefault("base_url", "")
        out.setdefault("api_key", "")
        out.setdefault("name", "") or None
    if out.get("name") == "":
        out.pop("name", None)  # 空名字回落到自动命名
    return out


def _auto_name(data: dict[str, Any]) -> str:
    name = str(data.get("name") or "").strip()
    if name:
        return name[:NAME_MAX]
    model = str(data.get("model") or "").strip() or "模型"
    return model[:NAME_MAX]


def create(db: Any, data: dict[str, Any]) -> dict[str, Any]:
    db = _global(db)
    values = validate(data)
    now = _now(db)
    existing = _rows(db, "SELECT COUNT(*) n FROM llm_profiles")[0]["n"]
    make_active = 1 if int(existing) == 0 else 0
    with db.write() as s:
        pid = _exec(s, "INSERT INTO llm_profiles(name,provider,base_url,model,api_key,active,created_at,updated_at) "
                       "VALUES(:n,:p,:b,:m,:k,:a,:c,:u)",
                   n=_auto_name(values), p=values["provider"], b=values.get("base_url", ""),
                   m=values["model"], k=values.get("api_key", ""), a=make_active, c=now, u=now)
    log.info("新增模型档案 id=%s provider=%s active=%s", pid, values["provider"], bool(make_active))
    return _mask(get(db, pid) or {})


def update(db: Any, pid: int, data: dict[str, Any]) -> dict[str, Any] | None:
    db = _global(db)
    from app.m2 import _bad

    row = get(db, pid)
    if row is None:
        return None
    values = validate(data, partial=True)
    if not values:
        return _mask(row)
    if values.get("model") == "":
        raise _bad("保存失败：模型名不能为空")
    if "name" in values:
        values["name"] = _auto_name({**row, **values})
    sets = ", ".join(f"{k}=:{k}" for k in values)
    with db.write() as s:
        _exec(s, f"UPDATE llm_profiles SET {sets}, updated_at=:u WHERE id=:i", u=_now(db), i=pid, **values)
    return _mask(get(db, pid) or {})


def delete(db: Any, pid: int) -> bool:
    db = _global(db)
    from sqlalchemy import text as sql

    row = get(db, pid)
    if row is None:
        return False
    with db.write() as s:
        s.execute(sql("DELETE FROM llm_profiles WHERE id=:i"), {"i": pid})
        if row["active"]:
            # 注意：这里必须用同一个 session 查，另开 session 会看到删除前快照，
            # 结果把刚删掉的那条又"启用"了一遍（UPDATE 影响 0 行）。
            nxt = s.execute(sql("SELECT id FROM llm_profiles ORDER BY id DESC LIMIT 1")).first()
            if nxt is not None:
                s.execute(sql("UPDATE llm_profiles SET active=1, updated_at=:u WHERE id=:i"),
                          {"u": _now(db), "i": nxt[0]})
    clear_refs(db, pid)  # 判定/对话若指着它，清掉回默认
    log.info("删除模型档案 id=%s（原本 %s）", pid, "生效中" if row["active"] else "未生效")
    return True


def activate(db: Any, pid: int) -> dict[str, Any] | None:
    db = _global(db)
    row = get(db, pid)
    if row is None:
        return None
    now = _now(db)
    with db.write() as s:
        _exec(s, "UPDATE llm_profiles SET active=0, updated_at=:u WHERE active=1", u=now)
        _exec(s, "UPDATE llm_profiles SET active=1, updated_at=:u WHERE id=:i", u=now, i=pid)
    log.info("切换模型档案 id=%s", pid)
    return _mask(get(db, pid) or {})


def mark_test(db: Any, pid: int, *, ok: bool, error: str = "") -> None:
    db = _global(db)
    with db.write() as s:
        _exec(s, "UPDATE llm_profiles SET last_test_at=:t, last_test_ok=:ok, last_test_error=:e WHERE id=:i",
              t=_now(db), ok=1 if ok else 0, e=(error or "")[:200], i=pid)


def migrate_legacy(db: Any, legacy: dict[str, str]) -> None:
    """把历史单配置搬成一条档案（只搬一次：表里已经有数据就跳过）。"""
    db = _global(db)
    try:
        count = int(_rows(db, "SELECT COUNT(*) n FROM llm_profiles")[0]["n"])
    except Exception:
        return
    if count:
        return
    api_key = (legacy.get("llm_api_key") or "").strip()
    model = (legacy.get("llm_model") or "").strip()
    if not api_key and not model:
        return
    provider = (legacy.get("llm_provider") or "openai").strip().lower()
    if provider not in PROVIDERS:
        provider = "openai"
    now = _now(db)
    with db.write() as s:
        _exec(s, "INSERT INTO llm_profiles(name,provider,base_url,model,api_key,active,created_at,updated_at) "
                 "VALUES(:n,:p,:b,:m,:k,1,:c,:u)",
              n=(model or "默认")[:NAME_MAX], p=provider, b=(legacy.get("llm_base_url") or "").strip(),
              m=model, k=api_key, c=now, u=now)
    log.info("已把历史模型配置迁移成默认档案（provider=%s）", provider)


def config_of(row: dict[str, Any]) -> Any:
    """档案 → LLMConfig。"""
    from app.llm import LLMConfig

    return LLMConfig(provider=row.get("provider", "openai"), base_url=row.get("base_url", ""),
                     api_key=row.get("api_key", ""), model=row.get("model", ""))

# ------------------------------------------------------------------ 判定 / 对话分别选模型

SEL_KEYS = {"judge": "judge_profile_id", "chat": "chat_profile_id"}


def selection(db: Any) -> dict[str, int | None]:
    """当前"判定用哪个 / 对话用哪个"（None 表示没单独指定，走默认）。"""
    db = _global(db)
    out: dict[str, int | None] = {}
    for purpose, key in SEL_KEYS.items():
        rows = _rows(db, "SELECT value FROM profile WHERE key=:k", k="setting." + key)
        raw = str(rows[0]["value"]).strip() if rows else ""
        try:
            out[purpose] = int(raw) if raw else None
        except ValueError:
            out[purpose] = None
    return out


def set_selection(db: Any, changes: dict[str, Any]) -> dict[str, int | None]:
    """只改传进来的那几项；值为 None 表示"清除，走默认"。id 不存在 → 400。"""
    db = _global(db)
    from app.m2 import _bad

    current = selection(db)
    now = _now(db)
    with db.write() as s:
        for purpose, key in SEL_KEYS.items():
            if purpose not in changes:
                continue
            value = changes[purpose]
            if value in (None, "", 0):
                current[purpose] = None
                _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,'',:t) "
                         "ON CONFLICT(key) DO UPDATE SET value='', updated_at=:t", k="setting." + key, t=now)
                continue
            try:
                pid = int(value)
            except (TypeError, ValueError):
                raise _bad("保存失败：模型选择必须是档案 id") from None
            if get(db, pid) is None:
                raise _bad("保存失败：模型档案不存在", 404)
            current[purpose] = pid
            _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,:v,:t) "
                     "ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t",
                  k="setting." + key, v=str(pid), t=now)
    log.info("模型选择：判定=%s 对话=%s", current.get("judge"), current.get("chat"))
    return current


def resolve(db: Any, purpose: str) -> dict[str, Any] | None:
    """按用途取该用的档案：显式选择 → 生效档案 → None（交给历史单配置兜底）。"""
    db = _global(db)
    pid = selection(db).get(purpose)
    if pid is not None:
        row = get(db, pid)
        if row is not None:
            return row
    return active(db)


def clear_refs(db: Any, pid: int) -> None:
    """档案被删后，把它从"判定/对话"选择里清掉，避免指向不存在的档案。"""
    db = _global(db)
    keep = [k for k, v in selection(db).items() if v == pid]
    if keep:
        set_selection(db, {k: None for k in keep})
