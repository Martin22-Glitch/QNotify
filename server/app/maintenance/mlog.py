"""maintenance_log：只在主库。写失败只记日志，绝不影响业务。"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from ..db import Database, utc_now_iso
from ..models import MAINTENANCE_LOG_STATEMENTS

log = logging.getLogger(__name__)


def _g(db: Database) -> Database:
    from ..accounts import global_db

    return global_db(db)


def ensure_table(db: Database) -> None:
    with _g(db).write() as s:
        conn = s.connection()
        for stmt in MAINTENANCE_LOG_STATEMENTS:
            conn.exec_driver_sql(stmt)


def write(db: Database | None, kind: str, detail: dict[str, Any], ok: bool) -> None:
    if db is None:
        return
    try:
        with _g(db).write() as s:
            s.execute(
                text("INSERT INTO maintenance_log (kind, detail, ok, created_at) VALUES (:k, :d, :o, :t)"),
                {"k": kind, "d": json.dumps(detail, ensure_ascii=False), "o": 1 if ok else 0, "t": utc_now_iso()},
            )
    except Exception:
        log.warning("写维护记录失败 kind=%s", kind, exc_info=True)


def recent(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with _g(db).session() as s:
        rows = s.execute(
            text("SELECT kind, detail, ok, created_at FROM maintenance_log ORDER BY id DESC LIMIT :n"), {"n": limit}
        ).all()
    out = []
    for r in rows:
        try:
            detail = json.loads(r.detail)
        except ValueError:
            detail = {"raw": r.detail}
        out.append({"kind": r.kind, "detail": detail, "ok": bool(r.ok), "created_at": r.created_at})
    return out
