"""启动自检：user_version、PRAGMA quick_check、表归类。

只检查、只记录（日志 ERROR + maintenance_log），不修复、不回滚、不挡启动。
结果挂在 app.state.integrity，供以后的自检接口 / 自动回滚读取。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from ..db import SCHEMA_VERSION, Database
from . import mlog

log = logging.getLogger(__name__)

KIND = "integrity_check"


def _probe(path: Path) -> dict[str, Any]:
    """只读连接探测，不经过 Database（避免触发迁移或缓存）。"""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        uv = int(conn.execute("PRAGMA user_version").fetchone()[0])
        qc = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
    finally:
        conn.close()
    return {"user_version": uv, "quick_check": qc}


def run(db: Database) -> dict[str, Any]:
    from .. import accounts

    base = accounts.global_db(db)
    base_path = Path(base.path).resolve()
    targets: list[tuple[str, Path, bool]] = [("base", base_path, True)]
    for r in accounts.list_all(base):
        p = accounts.db_path_of(base, r["id"])
        if p.exists() and p.resolve() != base_path:
            # 账号库可能在首次打开时才迁移，所以只要求"不高于当前版本"
            targets.append((f"account:{r['id']}", p.resolve(), False))

    problems: list[dict[str, Any]] = []
    for name, path, strict in targets:
        try:
            res = _probe(path)
        except sqlite3.Error as exc:
            problems.append({"target": name, "issue": "unreadable", "error": str(exc)[:200]})
            continue
        if res["quick_check"] != ["ok"]:
            problems.append({"target": name, "issue": "quick_check", "detail": res["quick_check"][:5]})
        uv = res["user_version"]
        if (strict and uv != SCHEMA_VERSION) or uv > SCHEMA_VERSION:
            problems.append({"target": name, "issue": "user_version", "found": uv, "expected": SCHEMA_VERSION})

    unknown = accounts.unclassified_tables(base)
    if unknown:
        problems.append({"target": "base", "issue": "unclassified_tables", "tables": unknown})

    ok = not problems
    if ok:
        log.info("启动自检通过（%d 个库）", len(targets))
    else:
        log.error("启动自检发现问题（服务继续运行，请尽快处理）：%s", problems)
        mlog.write(base, KIND, {"schema_version": SCHEMA_VERSION, "problems": problems}, False)
    return {"ok": ok, "checked": len(targets), "problems": problems}
