"""关注清单。

分工（ §5）：只进判定提示词；代码不按关键词执行，只认模型给的 focus，
且只接受清单里现存的话题——删掉的话题立即失效。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text as sql

from app import m2

log = logging.getLogger(__name__)
TOPIC_MAX_LEN = 20
LEGACY_KV = "mute_migrated_v1"   # 真实旧数据： 迁移时记录的被删规则
_QUOTES = "「」『』“”\"'‘’《》【】"

FOCUS_PROMPT = """【关注清单】用户说过想留意的话题：
{lines}
用法（只有这一种）：
1. 只用来填 focus：事项内容明显跟清单里某个话题有关，focus 就从清单里逐字照抄那个话题；无关填 ""。
2. 关注不改变 kind 和 audience_type。先只看消息本身判 kind 和 audience_type，再单独判 focus。
   不要因为跟关注话题有关就把 info 判成 directive，也不要因此把对象判成 me 或 role_me。
3. 消息没有拆出事项时，在顶层 focus 字段按同样规则填写。"""


def normalize(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    t = re.sub(r"\s+", " ", raw).strip().strip(_QUOTES).strip()
    return t if 0 < len(t) <= TOPIC_MAX_LEN else ""


def list_topics(db: Any) -> list[dict]:
    return m2._rows(db, "SELECT id, topic, note, created_at, hits FROM focus_topics ORDER BY id")


def topic_map(db: Any) -> dict[str, str]:
    return {str(r["topic"]).casefold(): str(r["topic"]) for r in list_topics(db)}


def canonical(raw: Any, topics: dict[str, str] | None) -> str:
    """校验模型给的 focus：必须是清单里现存的话题，否则视为空。不是关键词匹配。"""
    t = normalize(raw)
    return topics.get(t.casefold(), "") if t and topics else ""


def prompt_block(topics: dict[str, str]) -> str:
    if not topics:
        return '【关注清单】（空）所有 focus 一律填 ""。'
    return FOCUS_PROMPT.format(lines="\n".join(f"- {t}" for t in topics.values()))


def _insert(s: Any, topic: str, note: str, now: str) -> tuple[int, str]:
    row = s.execute(sql("SELECT id FROM focus_topics WHERE topic=:t"), {"t": topic}).first()
    if row:
        return int(row[0]), "unchanged"
    rid = m2._exec(s, "INSERT INTO focus_topics(topic,note,created_at,hits) VALUES(:t,:no,:n,0)",
                   t=topic, no=note, n=now)
    return int(rid), "created"


def add_from_chat(db: Any, raw: Any, user_text: str) -> list[tuple[dict | None, str, str]]:
    v = raw.get("topics", raw.get("topic")) if isinstance(raw, dict) else raw
    items = [v] if isinstance(v, str) else (v if isinstance(v, list) else [])
    if not items:
        return [(None, "dropped", "没识别出要关注的话题")]
    note, now = str(user_text or "").strip()[:200], m2.now_iso(db)
    out: list[tuple[int | None, str, str]] = []
    seen: set[str] = set()
    with db.write() as s:
        for it in items[:5]:
            t = normalize(it)
            if not t:
                out.append((None, "dropped", f"话题词为空或超过 {TOPIC_MAX_LEN} 字"))
                continue
            if t.casefold() in seen:
                continue
            seen.add(t.casefold())
            rid, outcome = _insert(s, t, note, now)
            out.append((rid, outcome, ""))
    rows = {int(r["id"]): r for r in list_topics(db)}
    return [(rows.get(rid) if rid else None, o, w) for rid, o, w in out]


def add_manual(db: Any, topic: Any) -> tuple[dict | None, str]:
    """手动添加一条关注；返回 (行, created/unchanged)。"""
    t = normalize(topic)
    if not t:
        return None, "dropped"
    with db.write() as s:
        rid, outcome = _insert(s, t, "手动添加", m2.now_iso(db))
    row = next((r for r in list_topics(db) if int(r["id"]) == rid), None)
    return row, outcome


def delete(db: Any, fid: int) -> dict | None:
    with db.write() as s:
        row = s.execute(sql("SELECT id, topic FROM focus_topics WHERE id=:i"), {"i": fid}).first()
        if not row:
            return None
        s.execute(sql("DELETE FROM focus_topics WHERE id=:i"), {"i": fid})
    return {"id": int(row[0]), "topic": str(row[1])}


def remove_from_chat(db: Any, raw: Any) -> tuple[dict | None, str]:
    """对话里删一条关注；返回 (被删的行, removed/absent/failed)。

    只做精确比对（casefold），与 canonical() 同一思路；删后从 list_topics 回读确认。
    本函数不发事件——推送由调用方（路由/意图层）决定。
    """
    t = normalize(raw)
    if not t:
        return None, "absent"
    row = next((r for r in list_topics(db) if str(r["topic"]).casefold() == t.casefold()), None)
    if row is None:
        return None, "absent"
    delete(db, int(row["id"]))
    gone = all(int(r["id"]) != int(row["id"]) for r in list_topics(db))
    return row, ("removed" if gone else "failed")


def bump_hits(s: Any, topics: set[str]) -> None:
    for t in topics:
        s.execute(sql("UPDATE focus_topics SET hits=hits+1 WHERE topic=:t"), {"t": t})


# ---------- 迁移：真实旧数据在 app_kv.mute_migrated_v1 ----------
def migrate_legacy(db: Any) -> int:
    """把  迁移时删掉的 review 规则转成关注话题（如「考研」），只做一次。"""
    rows = m2._rows(db, "SELECT value FROM app_kv WHERE key=:k", k=LEGACY_KV)
    if not rows:
        return 0
    if m2._rows(db, "SELECT value FROM app_kv WHERE key='focus_migrated_v1'"):
        return 0
    try:
        data = json.loads(str(rows[0]["value"] or "{}"))
    except ValueError:
        data = {}
    deleted = data.get("deleted") if isinstance(data, dict) else None
    topics = []
    for item in deleted or []:
        if isinstance(item, dict) and str(item.get("action") or "") == "review":
            t = normalize(item.get("keyword"))
            if t:
                topics.append(t)
    created = 0
    with db.write() as s:
        for t in dict.fromkeys(topics):
            created += _insert(s, t, "从旧规则迁移（关注）", m2.now_iso(db))[1] == "created"
        s.execute(sql("INSERT OR REPLACE INTO app_kv(key,value) VALUES('focus_migrated_v1','1')"))
    if topics:
        log.info("关注迁移：解析 %d 个，新增 %d 个", len(topics), created)
    return created
