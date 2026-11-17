"""M2-A：消息块聚合 → 规则粗筛 → 决策 → 待确认/待办 → 反馈与记忆样本 → 对话 → 画像。

挂载方式：create_app 末尾调用 install(app)。M2 路由会排到路由表最前面，
所以 /api/settings、/api/stats 由本模块接管，再调用 M1 原来的处理函数后合并字段。
本模块自身不发任何 HTTP 请求；只有 app.llm 存在且已配置时才交给它判定。

待办三态互转记样本、消息级人工改判（追加 stage=manual 决策）、
待确认带 AI 建议（判定时存下 items[0]，读取时不再调用模型）。

POST /api/chat/messages 的回复由 app.chat_llm 生成（受控上下文 + 预算裁剪），
失败/未配置走 CHAT_FALLBACK_REPLY；响应结构不变。

感知/决策分离。模型只报事实（kind、items[].audience_type），档位由本文件的
ROUTE_TABLE 决定，confidence 只做观测。每个事项单独路由，整块取最高档；
每条决策带 route={verdict, by, why} 落库并经 /api/decisions 返回。
模型输出没有新字段时回退到旧的阈值逻辑，行为与之前一致。

用户规则层（app.user_rules）。模型判定之后、落库之前套用 user_rules 表里
status='active' 的规则；命中即以规则为准，route.by=rule#id 并写明原因，模型原判保留在
route.model。没有命中时 r 原样返回，路由表与落库逻辑不变。规则粗筛（rule_ignore）不受影响。

对话 → 建议规则。chat_llm 剥掉回复里的 <<RULE>> 标记，chat_send 把解析出的
{match, action} 交给 user_rules.suggest_from_chat 落成 suggested（不生效）；
用户经 POST /api/agent/rules/{id}/confirm|reject 确认或拒绝。对话响应结构不变。

标记体系整体退役。对话正文只聊天；chat_intent 判断这一句要做什么，
intent_exec 调现有执行器落地并把拿不准的意图落成 chat_confirms 确认卡，
chat_send 只看执行器回读出来的回执（app.receipt_view），按纯聊天/写入/混合三选一拼回复。
步骤 4：说话层的 user_wants_change 与意图层的 pure_none 对账，有分歧就补一张 diverge 确认卡；
按触发表判断"要不要兜底"的旧门控与静默提示行已从对话链路移除。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql

from app import lifecycle, llm_profiles, models, repo, runtime_config
from app.api import deps
from app.auth import require_token, token_matches
from app.events import hub
from app.llm import ledger
from app.models import RETENTION_ACKED_AT_KEY

log = logging.getLogger("app.m2")

DDL = """
CREATE TABLE IF NOT EXISTS message_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, sender_id INTEGER NOT NULL,
    msg_ids TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', msg_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, processed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_blocks_pending ON message_blocks (processed_at);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, block_id INTEGER NOT NULL, msg_id INTEGER,
    stage TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('todo','review','ignored','failed')),
    reason TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', raw_response TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_decisions_block ON decisions (block_id);
CREATE INDEX IF NOT EXISTS idx_decisions_msg ON decisions (msg_id);
CREATE TABLE IF NOT EXISTS todo_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dropped')),
    confidence REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todo_items (status, created_at DESC);
CREATE TABLE IF NOT EXISTS todo_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, todo_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
    block_id INTEGER, group_id INTEGER, group_name TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL DEFAULT '',
    msg_time INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_sources_todo ON todo_sources (todo_id);
CREATE INDEX IF NOT EXISTS idx_sources_msg ON todo_sources (msg_id);
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, block_id INTEGER NOT NULL, msg_id INTEGER,
    confidence REAL NOT NULL DEFAULT 0, question TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','ignored')),
    created_at TEXT NOT NULL, resolved_at TEXT, suggestion TEXT);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items (status, created_at DESC);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK (action IN ('confirm','ignore','restore','edit')),
    review_id INTEGER, todo_id INTEGER, msg_id INTEGER, note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, group_id INTEGER,
    sender_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    kind TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text','question','notice','confirm')),
    text TEXT NOT NULL, ref_review_id INTEGER, ref_msg_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('rule','preference','fact')),
    content TEXT NOT NULL, norm TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','merged')),
    source TEXT NOT NULL DEFAULT 'manual', weight INTEGER NOT NULL DEFAULT 1,
    hits INTEGER NOT NULL DEFAULT 0, merged_into INTEGER, merged_from TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_hit_at TEXT, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_agent_memory_status ON agent_memory (status, kind);
CREATE TABLE IF NOT EXISTS agent_memory_turns (
    chat_id INTEGER NOT NULL, memory_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, memory_id));
CREATE INDEX IF NOT EXISTS idx_agent_memory_turns_mem ON agent_memory_turns (memory_id);
CREATE TABLE IF NOT EXISTS user_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, sender TEXT, keyword TEXT,
    action TEXT NOT NULL CHECK (action IN ('todo','review','ignored')),
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT 'manual', memory_id INTEGER, note TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_hit_at TEXT);
CREATE INDEX IF NOT EXISTS idx_user_rules_status ON user_rules (status, id);
CREATE INDEX IF NOT EXISTS idx_user_rules_memory ON user_rules (memory_id);
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)
"""

# 旧库补列：(表, 列, 类型)。CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，统一走这里。
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("chat_event_log", "extract_tried", "INTEGER NOT NULL DEFAULT 0"),   # 
    ("chat_event_log", "extract_result", "TEXT NOT NULL DEFAULT ''"),   # 
    ("review_items", "suggestion", "TEXT"),
    ("decisions", "route", "TEXT"),  # 
    ("todo_items", "origin", "TEXT NOT NULL DEFAULT 'message'"),  # 
    ("chat_messages", "ref_proposal_id", "INTEGER"),  # 
    # 对话来的待确认
    ("review_items", "source", "TEXT NOT NULL DEFAULT 'message'"),
    ("review_items", "chat_message_id", "INTEGER"),
    ("review_items", "ask_reason", "TEXT"),
    ("review_items", "time_text", "TEXT"),
    ("review_items", "dedupe_key", "TEXT"),
    # 意图层观测（三条都可空，旧行不受影响）
    ("chat_event_log", "intent_status", "TEXT"),
    ("chat_event_log", "intent_kinds", "TEXT"),
    ("chat_event_log", "intent_attempts", "INTEGER"),
)

CUR_COUNTER = "m2_last_message_rowid"
LLM_COUNTER = "m2_llm_calls"
SETTING_PREFIX = "setting."  # M2 运行时设置存在 profile 表里（键带前缀，画像接口不会读到）
LEGACY_PROFILE_KEYS = ("grade", "class_name", "major", "roles")  #  已退役，只给一次性迁移读
PROFILE_ABOUT_KEY = "about"
PROFILE_ABOUT_MAX_CHARS = 500
PROFILE_MIGRATED_KEY = "meta.about_migrated"  # 迁移标记；画像接口只读 about，不会暴露它
PROFILE_COOLDOWN = timedelta(hours=24)  # 闸门 5
PROFILE_EXPLICIT_WORD = "画像"           # 闸门 5 的例外：用户原话里有这个词就算明确要求
EVIDENCE_MIN_CHARS = 4                  # 折叠后少于 4 个字的"证据"什么都能命中，不认
EVIDENCE_MAX_ITEMS = 5
EVIDENCE_MAX_CHARS = 200
PROFILE_DROPPED_REPLY = "这次没有生成画像草稿。想更新画像的话，可以直接告诉我你的情况，或者去设置页自己改。"
SAMPLE_TEXT_MAX = 200  # 学习样本只记要点：原文截断到 200 字
MANUAL_VERDICTS = ("todo", "ignored")  # review 不是终态，不能人工设成它
CHAT_FALLBACK_REPLY = ("收到，我先记在对话里了。目前还没接上模型，没法自动采纳这条偏好；"
                       "你可以在待确认里逐条确认或忽略，这些决定都会记成样本。")
_STATE: dict[int, dict[str, Any]] = {}  # 兼容旧代码；真实状态挂在 db 对象上（见 _state）

IGNORE_WORDS = {"收到", "好的", "好", "嗯", "ok", "1", "1.", "+1", "6"}
EXEMPT_WORDS = ("务必", "截止", "前提交", "截止时间", "@所有人")


# ------------------------------------------------------------------  路由表
# 模型说事实（kind / audience_type / 有没有 due_at），这里定档位。confidence 不参与。
# 自上而下第一条匹配的规则生效；"*" 表示任意。改口味就改这张表。
ROUTE_KINDS = ("directive", "optional", "info", "chat", "noise")
AUDIENCE_TYPES = ("me", "all", "conditional", "role_me", "role", "other", "unknown")

# （D18-4）：能进待办的任务必须时间明确。
NO_DUE_WHY = "要求你做事，但没说什么时候"

ROUTE_TABLE: tuple[tuple[str, str, str, str, str], ...] = (
    # kind         audience_type   截止      去向       为什么
    ("chat",       "*",            "*",      "ignored", "闲聊，不需要处理"),
    ("noise",      "*",            "*",      "ignored", "表情、接龙或应答，没有信息量"),
    ("info",       "*",            "*",      "ignored", "通知或宣传，不要求做什么"),
    ("directive",  "me",           "due",    "todo",    "明确要求本人做事"),
    ("directive",  "me",           "no_due", "review",  NO_DUE_WHY),
    ("directive",  "all",          "due",    "todo",    "明确要求所有人做事"),
    ("directive",  "all",          "no_due", "review",  NO_DUE_WHY),
    ("directive",  "conditional",  "*",      "review",  "有条件的要求，需要你确认是否适用"),
    ("directive",  "role_me",      "due",    "todo",    "面向某身份的要求，画像表明你就是这个身份"),
    ("directive",  "role_me",      "no_due", "review",  NO_DUE_WHY),
    ("directive",  "role",         "*",      "review",  "面向某个身份的要求，还不知道你是不是这个身份"),
    ("directive",  "other",        "*",      "review",  "看起来是针对别人的要求，请确认"),
    ("directive",  "unknown",      "*",      "review",  "看不出这件事针对谁"),
    ("optional",   "*",            "due",    "review",  "自愿参与但有截止，由你决定参不参加"),
    ("optional",   "*",            "no_due", "ignored", "自愿参与且没有截止，不打扰"),
)
ROUTE_DEFAULT = ("review", "没有匹配的路由规则，保守起见请确认")

# ⚠ 必须和 /api/review/{id}/resolve 确认时写入的状态值一致（review_items.status 可能有 CHECK）
REJUDGE_REVIEW_STATUS = "confirmed"
PROFILE_EVIDENCE_MIN = 4
VERDICT_RANK = {"ignored": 0, "review": 1, "todo": 2}
VERDICT_LABELS = {"todo": "进待办", "review": "待确认", "ignored": "忽略"}
SHOW_AUDIENCE_TEXT = ("conditional", "role", "other", "unknown")


# ------------------------------------------------------------------ 基础工具
def _global(db):
    """通用资产（模型档案/系统设置/账本）只在主库，不跟账号走。"""
    from .accounts import global_db

    return global_db(db)

def _ctx_db(request: Request) -> tuple[Any, Any]:
    ctx = request.app.state.ctx
    db = deps.get_db(ctx)
    ensure_schema(db)
    return ctx, db


def _state(db: Any) -> dict[str, Any]:
    """按 db 对象取状态。

    以前用 id(db) 当键：对象被回收后 id 可能被新对象复用，于是 ensure_schema
    以为表已建好而跳过建表，报 "no such table"（偶发、难查）。现在挂在对象自己身上。
    """
    st = getattr(db, "_m2_state", None)
    if st is None:
        st = {"schema": False, "last_error": None}
        try:
            db._m2_state = st
        except Exception:  # 极端情况下对象不允许挂属性，退回按 id 存
            st = _STATE.setdefault(id(db), {"schema": False, "last_error": None})
    return st


MUTE_MIGRATION_KEY = "mute_migrated_v1"
EXAM_SENTENCE = "正在备考研究生。"


def migrate_mute_rules(db: Any) -> bool:
    """退役判定型规则。真正动了库返回 True。

    - ignored 规则原样保留
    - 关键词/备注含"考研"的判定型规则 → 画像补一句（已有备考/考研相关内容就不加），再删除
    - 其它 todo/review 规则 → 删除（快照存进 app_kv，不丢信息）
    - 标记只在真正迁移后写：空库首次启动不写标记（新规则已经进不来判定型动作）
    - 画像写失败 → 不删规则、不写标记、返回 False；残留规则不会生效（find 只取 ignored），下次启动重试
    """
    from app import user_rules

    if _rows(db, "SELECT key FROM app_kv WHERE key=:k", k=MUTE_MIGRATION_KEY):
        return False
    legacy = _rows(db, "SELECT id, group_id, sender, keyword, action, status, note FROM user_rules "
                       "WHERE action <> 'ignored' ORDER BY id")
    if not legacy:
        return False
    exam = [r for r in legacy
            if "考研" in user_rules._fold(f"{r.get('keyword') or ''} {r.get('note') or ''}")]
    profile_added = False
    if exam:
        current = str(_profile(db).get(PROFILE_ABOUT_KEY) or "")
        if not any(w in current for w in ("备考", "考研")):
            joiner = "" if not current.strip() or current.rstrip()[-1] in "。！？.!?" else "。"
            new = _normalize_about(current.rstrip() + joiner + EXAM_SENTENCE)
            if len(new) > PROFILE_ABOUT_MAX_CHARS:
                log.warning("「不看」迁移：画像已满，补不进备考那句，规则暂不删除")
                return False
            ev = [f"规则 #{r['id']}：{r.get('note') or r.get('keyword')}" for r in exam]
            written, why = _profile_write(db, new, evidence=ev, source="migration")
            if written is None:
                log.warning("「不看」迁移：画像写入失败（%s），规则暂不删除", why)
                return False
            profile_added = True
    now = now_iso(db)
    marker = {"at": now, "deleted": legacy, "profile_added": profile_added,
              "focus_topics_todo": ["考研"] if exam else []}  # 给  建关注话题用
    with db.write() as s:
        for r in legacy:
            s.execute(sql("DELETE FROM user_rules WHERE id=:i"), {"i": r["id"]})
        s.execute(sql("INSERT OR REPLACE INTO app_kv(key, value) VALUES(:k, :v)"),
                  {"k": MUTE_MIGRATION_KEY, "v": json.dumps(marker, ensure_ascii=False, default=str)})
    log.info("「不看」迁移完成：删除判定型规则 %s，画像补句=%s",
             [r["id"] for r in legacy], profile_added)
    return True


def ensure_schema(db: Any) -> None:
    st = _state(db)
    if st["schema"]:
        return
    body = "\n".join(line.split("--")[0] for line in DDL.splitlines())
    with db.engine.begin() as conn:
        for stmt in body.split(";"):
            if stmt.strip():
                conn.exec_driver_sql(stmt)
        for stmt in llm_profiles.DDL.split(";"):
            if stmt.strip():
                conn.exec_driver_sql(stmt)
        for stmt in ledger.DDL.split(";"):
            if stmt.strip():
                conn.exec_driver_sql(stmt)
        for stmt in models.PROFILE_HISTORY_STATEMENTS:  # 
            conn.exec_driver_sql(stmt)
        for stmt in (*models.RULE_HITS_STATEMENTS, *models.APP_KV_STATEMENTS,
                     *models.RETIRED_STATEMENTS):  # 
            conn.exec_driver_sql(stmt)
        for stmt in models.CHAT_EVENT_LOG_STATEMENTS:  # 事件观测
            conn.exec_driver_sql(stmt)
        for stmt in models.FOCUS_TOPIC_STATEMENTS:  # 关注清单
            conn.exec_driver_sql(stmt)
        for stmt in models.CHAT_CONFIRMS_STATEMENTS:  # 意图层确认卡
            conn.exec_driver_sql(stmt)
        _migrate(conn)
        _widen_chat_message_kinds(conn)
        conn.exec_driver_sql(
            "UPDATE todo_items SET origin='chat' WHERE origin='message' AND detail='来自对话' "
            "AND NOT EXISTS (SELECT 1 FROM todo_sources s WHERE s.todo_id = todo_items.id)")
    st["schema"] = True
    try:  # 旧规则里的「考研」等转为关注话题（只做一次）
        from app import focus_topics

        focus_topics.migrate_legacy(db)
    except Exception as exc:
        log.warning("关注迁移失败：%s", type(exc).__name__)
    try:
        legacy = {"llm_provider": m2_settings(db)["llm_provider"]}
        kv = {r["key"]: r["value"] for r in _rows(db, "SELECT key, value FROM settings")}
        legacy.update({k: kv.get(k, "") for k in ("llm_base_url", "llm_model", "llm_api_key")})
        llm_profiles.migrate_legacy(db, legacy)
    except Exception as exc:
        log.warning("模型档案迁移失败（忽略）：%s", type(exc).__name__)
    try:  # 每个账号库在本进程里只跑一次
        migrate_mute_rules(db)
    except Exception as exc:
        log.warning("「不看」规则迁移失败（规则原样保留，下次启动重试）：%s", type(exc).__name__)


def _migrate(conn: Any) -> None:
    """给旧库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。只加列，不删不改。"""
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


CHAT_MESSAGE_TABLE_COLS = ("id", "role", "kind", "text", "ref_review_id", "ref_msg_id",
                           "ref_proposal_id", "created_at")


def _chat_message_ddl(name: str) -> str:
    return (f"CREATE TABLE IF NOT EXISTS {name} ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " role TEXT NOT NULL CHECK (role IN ('user','assistant')),"
            " kind TEXT NOT NULL DEFAULT 'text'"
            " CHECK (kind IN ('text','question','notice','confirm')),"
            " text TEXT NOT NULL, ref_review_id INTEGER, ref_msg_id INTEGER,"
            " ref_proposal_id INTEGER, created_at TEXT NOT NULL)")


def _widen_chat_message_kinds(conn: Any) -> None:
    """旧库的 chat_messages 有 CHECK(kind IN ('text','question','notice'))，
    确认卡要写 kind='confirm'。SQLite 改不了 CHECK，只能重建表后把旧行原样搬回。

    可重复执行：新库/已重建过的库，建表语句里已经有 'confirm'，直接返回。
    """
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_messages'").first()
    if row is None or "confirm" in str(row[0] or ""):
        return
    old_cols = [str(r[1]) for r in conn.exec_driver_sql("PRAGMA table_info(chat_messages)")]
    keep = [c for c in CHAT_MESSAGE_TABLE_COLS if c in old_cols]
    cols = ", ".join(keep)
    conn.exec_driver_sql(_chat_message_ddl("chat_messages_046"))
    conn.exec_driver_sql(f"INSERT INTO chat_messages_046({cols}) SELECT {cols} FROM chat_messages")
    conn.exec_driver_sql("DROP TABLE chat_messages")
    conn.exec_driver_sql("ALTER TABLE chat_messages_046 RENAME TO chat_messages")
    log.info("chat_messages 已重建：kind 允许 'confirm'（旧行保留列 %s）", cols)


def _rows(db: Any, q: str, **p: Any) -> list[dict[str, Any]]:
    with db.session() as s:
        return [dict(r._mapping) for r in s.execute(sql(q), p)]


def _exec(s: Any, command: str, **p: Any) -> int:
    return s.execute(sql(command), p).lastrowid


def _pick(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _dump(x: Any) -> dict:
    return x.model_dump() if hasattr(x, "model_dump") else dict(x)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _tz(db: Any) -> ZoneInfo:
    try:
        return ZoneInfo(m2_settings(db)["timezone"])
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _today(db: Any) -> str:
    """今天的日期。QN_FAKE_TODAY 可冻结：检查要可复现，同一条"周五前交"不能今天和明天不同答案。"""
    fake = os.environ.get("QN_FAKE_TODAY", "").strip()
    if fake:
        return fake[:10]
    return datetime.now(_tz(db)).date().isoformat()


def _expiry_policy(db: Any) -> Any:
    """视图口径：overdue_days 读主库 retention.*，时区与 _today 同源。"""
    return lifecycle.make_policy(_tz(db), runtime_config.load_retention(db).overdue_days)


def now_iso(db: Any) -> str:
    return datetime.now(_tz(db)).isoformat(timespec="seconds")


def _bad(msg: str, code: int = 400) -> HTTPException:
    return HTTPException(status_code=code, detail=msg)


def _clip(text: Any) -> str:
    return str(text or "")[:SAMPLE_TEXT_MAX]


# ------------------------------------------------------------------ M2 设置
def _bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def m2_settings(db: Any) -> dict[str, Any]:
    db = _global(db)
    stored = {
        r["key"][len(SETTING_PREFIX):]: r["value"]
        for r in _rows(db, "SELECT key, value FROM profile WHERE key LIKE 'setting.%'")
    }

    def get(k: str, env: str, default: str) -> str:
        return stored.get(k, _env(env, default))

    try:
        thr = float(get("confidence_threshold", "QN_CONFIDENCE_THRESHOLD", "0.7"))
    except ValueError:
        thr = 0.7
    try:
        win = int(get("aggregate_window_seconds", "QN_AGGREGATE_WINDOW_SECONDS", "45"))
    except ValueError:
        win = 45
    return {
        "llm_provider": get("llm_provider", "QN_LLM_PROVIDER", "openai") or "openai",
        "pipeline_enabled": _bool(get("pipeline_enabled", "QN_PIPELINE_ENABLED", "true"), True),
        "confidence_threshold": thr,
        "aggregate_window_seconds": win,
        "timezone": _env("QN_TZ", "Asia/Shanghai") or "Asia/Shanghai",
        "notify_review_queue": _bool(get("notify_review_queue", "QN_NOTIFY_REVIEW_QUEUE", "false"), False),
        # 思考模式开关（关掉约省一半钱）
        "judge_thinking": _bool(get("judge_thinking", "QN_JUDGE_THINKING", "true"), True),
        "chat_thinking": _bool(get("chat_thinking", "QN_CHAT_THINKING", "true"), True),
    }


def validate_m2_settings(body: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    if body.get("llm_provider") is not None:
        if body["llm_provider"] not in ("openai", "anthropic"):
            raise _bad("设置保存失败：llm_provider 只能是 openai 或 anthropic")
        out["llm_provider"] = body["llm_provider"]
    if body.get("pipeline_enabled") is not None:
        out["pipeline_enabled"] = "true" if _bool(body["pipeline_enabled"], False) else "false"
    if body.get("notify_review_queue") is not None:
        out["notify_review_queue"] = "true" if _bool(body["notify_review_queue"], False) else "false"
    if body.get("confidence_threshold") is not None:
        try:
            v = float(body["confidence_threshold"])
        except (TypeError, ValueError):
            raise _bad("设置保存失败：confidence_threshold 必须是 0~1 的数字") from None
        if not 0 <= v <= 1:
            raise _bad("设置保存失败：confidence_threshold 必须在 0~1 之间")
        out["confidence_threshold"] = str(v)
    if body.get("aggregate_window_seconds") is not None:
        try:
            w = int(body["aggregate_window_seconds"])
        except (TypeError, ValueError):
            raise _bad("设置保存失败：aggregate_window_seconds 必须是整数") from None
        if not 0 <= w <= 600:
            raise _bad("设置保存失败：aggregate_window_seconds 必须在 0~600 之间")
        out["aggregate_window_seconds"] = str(w)
    if body.get("judge_thinking") is not None:  # 
        out["judge_thinking"] = "true" if _bool(body["judge_thinking"], True) else "false"
    if body.get("chat_thinking") is not None:
        out["chat_thinking"] = "true" if _bool(body["chat_thinking"], True) else "false"
    return out


def save_m2_settings(db: Any, values: dict[str, str]) -> None:
    db = _global(db)
    now = now_iso(db)
    with db.write() as s:
        for k, v in values.items():
            _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,:v,:t) "
                     "ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t",
                  k=SETTING_PREFIX + k, v=v, t=now)


def llm_config(db: Any, ctx: Any, purpose: str = "judge") -> tuple[Any, str, str]:
    """返回 (LLMConfig 或 None, provider, model)。app.llm 不存在 → None。"""
    db = _global(db)
    prof = llm_profiles.resolve(db, purpose)  # 按用途取档案（判定/对话可不同）
    if prof is not None:
        try:
            cfg = llm_profiles.config_of(prof)
        except ImportError:
            return None, prof.get("provider", ""), prof.get("model", "")
        return cfg, prof.get("provider", ""), prof.get("model", "")
    provider = m2_settings(db)["llm_provider"]
    base = _env("QN_LLM_BASE_URL", "")
    key = _env("QN_LLM_API_KEY", "")
    model = _env("QN_LLM_MODEL", "")
    try:
        from app import runtime_config

        cfg = runtime_config.load(db, ctx.settings)
        base = getattr(cfg, "llm_base_url", base) or base
        key = getattr(cfg, "llm_api_key", key) or key
        model = getattr(cfg, "llm_model", model) or model
    except Exception:
        pass
    try:
        from app.llm import LLMConfig
    except ImportError:
        return None, provider, model
    return LLMConfig(provider=provider, base_url=base, api_key=key, model=model), provider, model


# ------------------------------------------------------------------ 规则粗筛
def rule_ignore(text: str) -> str | None:
    t = "".join(text.split())
    if any(w in t for w in EXEMPT_WORDS):
        return None
    if t.lower() in IGNORE_WORDS:
        return "无信息量的应答词"
    if len(t) <= 2:
        return "正文过短（≤2 字符）"
    if not any(c.isalnum() or "\u4e00" <= c <= "\u9fff" for c in t):
        return "纯表情或纯标点"
    if "接龙" in t and len(t) < 40:
        return "接龙列表"
    if t.startswith("#忽略"):
        return "以 #忽略 开头"
    return None


# ------------------------------------------------------------------ 消息信息
def _group_names(db: Any) -> dict[int, str]:
    try:
        with db.session() as s:
            return {int(g["group_id"]): g.get("group_name", "") for g in repo.list_groups(s)}
    except Exception:
        return {}


def msg_infos(db: Any, msg_ids: list[int]) -> list[dict[str, Any]]:
    if not msg_ids:
        return []
    names = _group_names(db)
    ph = ",".join(str(int(m)) for m in msg_ids)
    rows = _rows(db, f"SELECT * FROM messages WHERE msg_id IN ({ph}) ORDER BY time, msg_id")
    out = []
    for r in rows:
        gid = r.get("group_id")
        out.append({
            "msg_id": int(r["msg_id"]),
            "group_id": gid,
            "group_name": names.get(int(gid), "") if gid is not None else "",
            "sender_name": str(_pick(r, "sender_name", "card", "nickname")),
            "text": str(_pick(r, "text", "content", "raw_message")),
            "time": int(_pick(r, "time", "msg_time", default=0) or 0),
        })
    return out


# ------------------------------------------------------------------ 管线
def build_blocks(db: Any) -> None:
    last = db.get_counter(CUR_COUNTER)
    rows = _rows(db, "SELECT rowid AS _rid, * FROM messages WHERE rowid > :i ORDER BY rowid", i=last)
    if not rows:
        return
    db.incr_counter(CUR_COUNTER, int(rows[-1]["_rid"]) - last)
    cfg = m2_settings(db)
    if not cfg["pipeline_enabled"]:
        return  # 管线关闭：照常入库，但不成块、不判定
    window = cfg["aggregate_window_seconds"]
    rows.sort(key=lambda r: (int(_pick(r, "time", default=0) or 0), r["_rid"]))
    blocks: list[dict[str, Any]] = []
    open_by_key: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        gid = r.get("group_id")
        uid = int(_pick(r, "user_id", "sender_id", default=0) or 0)
        t = int(_pick(r, "time", default=0) or 0)
        key = (gid, uid)
        b = open_by_key.get(key)
        if b and t - b["last"] <= window:
            b["ids"].append(int(r["msg_id"]))
            b["texts"].append(str(_pick(r, "text", "content", "raw_message")))
            b["last"] = t
        else:
            b = {"gid": gid, "uid": uid, "ids": [int(r["msg_id"])],
                 "texts": [str(_pick(r, "text", "content", "raw_message"))], "last": t}
            open_by_key[key] = b
            blocks.append(b)
    now = now_iso(db)
    with db.write() as s:
        for b in blocks:
            _exec(s, "INSERT INTO message_blocks(group_id,sender_id,msg_ids,text,msg_count,created_at) "
                     "VALUES(:g,:u,:m,:t,:c,:n)",
                  g=b["gid"], u=b["uid"], m=json.dumps(b["ids"]), t="\n".join(b["texts"]),
                  c=len(b["ids"]), n=now)


def _profile_rows(db: Any) -> dict[str, Any]:
    return {r["key"]: r for r in _rows(db, "SELECT key, value, updated_at FROM profile")}


def _profile(db: Any) -> dict[str, Any]:
    row = _profile_rows(db).get(PROFILE_ABOUT_KEY)
    return {
        "about": (row["value"] if row is not None else "") or "",
        "updated_at": (row["updated_at"] if row is not None else "") or "",
    }








def profile_about(db: Any) -> str:
    """判定/助手提示词用：画像原文（可能为空串）。"""
    return _profile(db)["about"]


def _normalize_about(v: str) -> str:
    return v.replace("\r\n", "\n").replace("\r", "\n").strip()


def _legacy_about_sentence(kv: dict[str, str]) -> str:
    parts: list[str] = []
    for key, label in (("grade", "年级"), ("class_name", "班级"), ("major", "专业")):
        v = " ".join(str(kv.get(key) or "").split())
        if v:
            parts.append(f"{label}是{v}")
    raw_roles = kv.get("roles") or ""
    try:
        roles = json.loads(raw_roles) if raw_roles else []
    except ValueError:
        roles = [raw_roles]
    if not isinstance(roles, list):
        roles = []
    names = [" ".join(str(r).split()) for r in roles if str(r).strip()]
    if names:
        parts.append("身份是" + "、".join(names))
    if not parts:
        return ""
    return ("我的" + "，".join(parts) + "。")[:PROFILE_ABOUT_MAX_CHARS]


def migrate_profile_about(db: Any) -> bool:
    """ 一次性迁移：about 为空且旧字段有值时拼成一句。返回是否真的写入了。

    - 已有迁移标记 → 什么都不做（用户迁移后清空 about，也不会被旧字段再填回去）
    - about 已有内容 → 只补标记，绝不覆盖
    - 旧字段全空 → 什么都不做，也不补标记
    """
    with db.session() as sess:
        has_table = sess.execute(sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile'")).first()
    if not has_table:
        return False
    rows = _profile_rows(db)
    if PROFILE_MIGRATED_KEY in rows:
        return False
    now = now_iso(db)
    about_row = rows.get(PROFILE_ABOUT_KEY)
    if about_row is not None and str(about_row["value"] or "").strip():
        with db.write() as s:
            _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,'1',:t) "
                     "ON CONFLICT(key) DO NOTHING", k=PROFILE_MIGRATED_KEY, t=now)
        return False
    sentence = _legacy_about_sentence({k: str(rows[k]["value"] or "") for k in LEGACY_PROFILE_KEYS if k in rows})
    if not sentence:
        return False
    with db.write() as s:
        # WHERE 兜底：万一同时有 PUT 写进了内容，不覆盖
        _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,:v,:t) "
                 "ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t WHERE trim(profile.value)=''",
              k=PROFILE_ABOUT_KEY, v=sentence, t=now)
        _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,'1',:t) "
                 "ON CONFLICT(key) DO NOTHING", k=PROFILE_MIGRATED_KEY, t=now)
    return True


# ------------------------------------------------------------------  路由


def _squash(s: Any) -> str:
    return " ".join(str(s or "").split())


def _profile_write(db: Any, about: str, *, evidence: list[str], source: str) -> tuple[dict | None, str]:
    """写 profile.about 并记 profile_history。内容没变 → (None, 原因)，不记历史。
    source='manual'（PUT）时内容没变也照旧刷新 updated_at，保持原 PUT 行为。"""
    now = now_iso(db)
    hid: int | None = None
    with db.write() as s:
        row = s.execute(sql("SELECT value FROM profile WHERE key=:k"), {"k": PROFILE_ABOUT_KEY}).first()
        before = str(row[0] or "") if row is not None else ""
        changed = before != about
        if changed or source == "manual":
            _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,:v,:t) "
                     "ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t",
                  k=PROFILE_ABOUT_KEY, v=about, t=now)
        # 写过就补迁移标记：之后旧字段迁移永远不会再动 about
        _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,'1',:t) "
                 "ON CONFLICT(key) DO NOTHING", k=PROFILE_MIGRATED_KEY, t=now)
        if changed:
            hid = _exec(s, "INSERT INTO profile_history(before_about,after_about,evidence,source,created_at) "
                           "VALUES(:b,:a,:e,:src,:n)",
                        b=before, a=about, e=json.dumps(evidence, ensure_ascii=False), src=source, n=now)
    if not changed:
        return None, "和现在的「关于我」一样，没有改动"
    log.info("画像写入：history=%s source=%s %d→%d 字", hid, source, len(before), len(about))
    return {"history_id": int(hid), "before": before, "about": about}, ""


def profile_write_from_chat(db: Any, raw: Any, user_text: str) -> tuple[dict | None, str]:
    """<<PROFILE>> → 直接写画像。返回 (写入结果, 丢弃原因)。闸门不过只记日志，不抛异常。"""
    def drop(why: str) -> tuple[None, str]:
        log.info("画像标记被丢弃：%s", why)
        return None, why

    if not isinstance(raw, dict):
        return drop("画像格式不对")
    v = raw.get("about")
    if not isinstance(v, str):
        return drop("画像格式不对")
    about = _normalize_about(v)
    if not about:
        return drop("画像内容是空的")
    if len(about) > PROFILE_ABOUT_MAX_CHARS:
        return drop(f"画像超过 {PROFILE_ABOUT_MAX_CHARS} 字")
    ev = raw.get("evidence")
    if isinstance(ev, str):
        ev = [ev]
    if not isinstance(ev, list) or not ev:
        return drop("没有拿你的原话作依据")
    said = _squash(user_text)
    cleaned: list[str] = []
    for e in ev:
        e2 = _squash(e) if isinstance(e, str) else ""
        if len(e2) < PROFILE_EVIDENCE_MIN or e2 not in said:
            return drop("依据不是你这条消息里的原话")
        cleaned.append(e2)
    return _profile_write(db, about, evidence=cleaned, source="chat")


def profile_undo(db: Any) -> dict | None:
    """回滚最近一条未撤销的变更：about ← before_about。没有可撤销 → None。一次只退一步。"""
    now = now_iso(db)
    with db.write() as s:
        row = s.execute(sql("SELECT id, before_about, after_about FROM profile_history "
                            "WHERE undone_at IS NULL ORDER BY id DESC LIMIT 1")).first()
        if row is None:
            return None
        hid, before, after = int(row[0]), str(row[1] or ""), str(row[2] or "")
        cur = s.execute(sql("SELECT value FROM profile WHERE key=:k"), {"k": PROFILE_ABOUT_KEY}).first()
        if (str(cur[0] or "") if cur is not None else "") != after:
            log.warning("画像撤销：当前内容与 history#%s 的 after 不一致（有未记历史的写入），仍按记录回滚", hid)
        _exec(s, "INSERT INTO profile(key,value,updated_at) VALUES(:k,:v,:t) "
                 "ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t", k=PROFILE_ABOUT_KEY, v=before, t=now)
        _exec(s, "UPDATE profile_history SET undone_at=:n WHERE id=:i", n=now, i=hid)
    log.info("画像撤销：history#%s", hid)
    return {"about": before, "undone_id": hid}


def profile_can_undo(db: Any) -> bool:
    return bool(_rows(db, "SELECT id FROM profile_history WHERE undone_at IS NULL LIMIT 1"))


def profile_undone_recent(db: Any, limit: int = 1) -> list[str]:
    """对话提示词用：最近被撤销掉的版本（截断）。"""
    rows = _rows(db, "SELECT after_about FROM profile_history WHERE undone_at IS NOT NULL "
                     "ORDER BY undone_at DESC, id DESC LIMIT :l", l=limit)
    return [str(r["after_about"] or "")[:300] for r in rows if str(r["after_about"] or "").strip()]

def _norm(value: Any, allowed: tuple[str, ...]) -> str | None:
    """枚举字段规范化：不是字符串或不在取值范围内 → None（视同缺失）。"""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in allowed else None


def _item_conf(item: dict, default: float = 0.0) -> float:
    try:
        return float(item.get("confidence", default) or 0)
    except (TypeError, ValueError):
        return default


def _route_info(verdict: str, by: str, why: str, **extra: Any) -> dict[str, Any]:
    return {"verdict": verdict, "by": by, "why": why, **extra}


def _is_structured(d: dict) -> bool:
    """模型是否给出了新字段。kind 与所有 audience_type 都缺失（或取值非法）→ 走旧阈值逻辑。"""
    if _norm(d.get("kind"), ROUTE_KINDS):
        return True
    return any(isinstance(i, dict) and _norm(i.get("audience_type"), AUDIENCE_TYPES)
               for i in (d.get("items") or []))


def _match_route(kind: str, audience: str, has_due: bool) -> tuple[str, str, str]:
    """查 ROUTE_TABLE，返回 (verdict, by, why)。by 由命中行的非通配列拼成，如 directive+me、optional+due。"""
    due = "due" if has_due else "no_due"
    for k, a, d, verdict, why in ROUTE_TABLE:
        if k == kind and a in ("*", audience) and d in ("*", due):
            by = "+".join(x for x in (k, "" if a == "*" else a, "" if d == "*" else d) if x)
            return verdict, by, why
    return ROUTE_DEFAULT[0], f"{kind}+{audience}", ROUTE_DEFAULT[1]



# （ §3）：关注只把"忽略"改成"待确认"，directive 一律不读 focus
FOCUS_ROUTE_TABLE: tuple[tuple[str, str, str, str], ...] = (
    ("info",      "*",      "review", "跟你关注的「{focus}」有关"),
    ("optional",  "no_due", "review", "自愿参与、没有截止，但跟你关注的「{focus}」有关"),
)
FOCUS_KINDS = frozenset(k for k, *_ in FOCUS_ROUTE_TABLE)


def _match_focus_route(kind: str, has_due: bool, focus: str) -> tuple[str, str, str] | None:
    due = "due" if has_due else "no_due"
    for k, d, verdict, why in FOCUS_ROUTE_TABLE:
        if k == kind and d in ("*", due):
            return verdict, f"focus:{k}/{d}", why.format(focus=focus)
    return None


def _route_item(block_kind: str | None, item: dict,
                focus_map: dict[str, str] | None = None) -> dict[str, Any]:
    """单个事项的路由。事项自带 kind（或 optional_hint）优先于整条消息的 kind。"""
    notes: list[str] = []
    kind = _norm(item.get("kind"), ROUTE_KINDS)
    hint = str(item.get("optional_hint") or "").strip()
    if not kind and hint:
        kind = "optional"
        notes.append(f"该事项标注为自愿参与（「{hint}」）")
    if not kind:
        kind = block_kind
    if not kind:
        kind = "directive"
        notes.append("模型没给 kind，按有事项推定为明确要求")
    audience = _norm(item.get("audience_type"), AUDIENCE_TYPES)
    if not audience:
        audience = "unknown"
        notes.append("模型没给 audience_type，按看不出对象处理")
    has_due = item.get("due_at") not in (None, "")
    verdict, by, why = _match_route(kind, audience, has_due)
    focus = ""
    if focus_map and kind in FOCUS_KINDS:
        from app import focus_topics as ft

        f = ft.canonical(item.get("focus"), focus_map)
        hit = _match_focus_route(kind, has_due, f) if f else None
        if hit and verdict == "ignored":
            verdict, by, why = hit
            focus = f
    audience_text = str(item.get("audience_text") or "").strip()
    if audience_text and (audience in SHOW_AUDIENCE_TEXT or kind == "optional"):
        why = f"{why}（「{audience_text}」）"
    if notes:
        why = f"{why}；{'；'.join(notes)}"
    return {"title": str(item.get("title") or ""), "verdict": verdict, "by": by, "why": why,
            "kind": kind, "audience_type": audience, "audience_text": audience_text,
            "focus": focus}






def _missing_due(rt: dict) -> bool:
    """这条待确认是不是因为"差时间"。只有 directive 的 no_due 行会命中。"""
    return (rt.get("kind") == "directive" and rt.get("verdict") == "review"
            and str(rt.get("by") or "").endswith("+no_due"))


def _guard_role_me(db: Any, r: dict) -> dict:
    """画像为空时模型仍给了 role_me → 按 role 重新路由。提示词约束之外的程序兜底。"""
    routed = r.get("routed")
    if not routed or not any(rt.get("audience_type") == "role_me" for _it, rt in routed):
        return r
    if profile_about(db).strip():
        return r
    fixed = []
    for it, rt in routed:
        if rt.get("audience_type") == "role_me":
            has_due = (it or {}).get("due_at") not in (None, "")
            v, by, why = _match_route(rt.get("kind") or "directive", "role", has_due)
            rt = {**rt, "verdict": v, "by": by, "audience_type": "role",
                  "why": f"{why}；画像是空的，不按「就是你」处理"}
        fixed.append((it, rt))
    out = {**r, "routed": fixed}
    if r.get("verdict") == "todo" and not any(rt.get("verdict") == "todo" for _it, rt in fixed):
        out["verdict"] = "review"
    log.info("role_me 兜底：画像为空，已降为 role")
    return out

def _route_structured(d: dict, focus_map: dict[str, str] | None = None) -> dict[str, Any]:
    """新格式：逐事项路由，整块取最高档。返回 verdict/confidence/item/route/routed。"""
    kind = _norm(d.get("kind"), ROUTE_KINDS)
    items = [i for i in (d.get("items") or []) if isinstance(i, dict)]
    if ((not d.get("actionable") or not items) and kind == "info" and focus_map
            and str(d.get("focus") or "").strip()):
        # 纯通知拆不出事项时，合成一个事项承载关注（否则关注对通知无效）
        items = [{"kind": "info", "audience_type": "all",
                  "title": str(d.get("evidence") or "")[:30] or "通知",
                  "focus": d.get("focus")}]
        d = {**d, "actionable": True}
    if not d.get("actionable") or not items:
        if kind in ("chat", "noise", "info"):
            _, by, why = _match_route(kind, "unknown", False)
        else:
            by, why = f"{kind or 'unknown'}+no_items", "模型没有抽出需要做的事项"
        return {"verdict": "ignored", "confidence": 1.0, "item": None, "routed": [],
                "route": _route_info("ignored", by, why, items=[])}
    routed = [(it, _route_item(kind, it, focus_map)) for it in items]
    best_item, best = max(routed, key=lambda x: VERDICT_RANK[x[1]["verdict"]])  # 并列取靠前的
    why = best["why"]
    if len(routed) > 1:
        counts = {v: sum(1 for _, rt in routed if rt["verdict"] == v) for v in ("todo", "review", "ignored")}
        parts = "、".join(f"{n} 条{VERDICT_LABELS[v]}" for v, n in counts.items() if n)
        why = f"{why}（共 {len(routed)} 个事项：{parts}；整块取最高档）"
    detail = [{k: rt[k] for k in ("title", "verdict", "by", "why")} for _, rt in routed]
    return {"verdict": best["verdict"], "confidence": min(_item_conf(i) for i in items),
            "item": best_item, "routed": routed,
            "route": _route_info(best["verdict"], best["by"], why, items=detail)}


async def _judge(db: Any, ctx: Any, block: dict, infos: list[dict]) -> dict[str, Any]:
    """返回 {stage, verdict, reason, confidence, evidence, provider, model, raw, item, route, routed}。

    routed：新格式下每个事项的 (item, 路由结果)；旧格式与降级为 None（走原来的单事项落库）。
    用户规则不在这里套：本函数只负责"模型怎么看 + 路由表怎么定"，规则层在 process_block 里接在后面。
    """
    cfg, provider, model = llm_config(db, ctx, "judge")
    fb = {"stage": "fallback", "verdict": "review", "reason": "llm_not_configured", "confidence": 0.0,
          "evidence": "", "provider": provider, "model": model, "raw": "", "item": None,
          "route": _route_info("review", "fallback", "模型还没配置，没法判断"), "routed": None}
    if cfg is None or not getattr(cfg, "configured", False):
        return fb
    from app.llm import judge

    first = infos[0] if infos else {}
    prof_about = profile_about(db)  # 提示词只收一段文本
    samples = _rows(db, "SELECT s.source, s.group_id, COALESCE(g.group_name, '') AS group_name, "
                        "s.sender_name, s.text, s.verdict, s.note "
                        "FROM memory_samples s LEFT JOIN groups g ON g.group_id = s.group_id "
                        "ORDER BY (s.group_id = :g) DESC, s.id DESC LIMIT 5", g=block["group_id"])
    think = bool(m2_settings(db)["judge_thinking"])  # 设置页可关
    from app import focus_topics as ft

    focus_map = ft.topic_map(db)
    res = await judge(config=cfg, text=block["text"], sender_name=first.get("sender_name", ""),
                      group_name=first.get("group_name", ""), profile=prof_about,
                      focus=ft.prompt_block(focus_map), samples=samples,
                      today=_today(db), thinking=think)
    ledger.record(db, purpose="judge", usage=res.usage or {}, provider=res.provider or provider,
                  model=res.model or model, thinking=think, block_id=block.get("id"))
    st = _state(db)
    if res.error != "not_configured":
        db.incr_counter(LLM_COUNTER, 1)
    if not res.ok:
        st["last_error"] = res.error
        fb["reason"] = res.error or "llm_error"
        fb["raw"] = res.raw or ""
        fb["route"] = _route_info("review", "fallback", f"模型调用失败（{fb['reason']}），先请你确认")
        return fb
    st["last_error"] = None
    d = res.decision or {}
    items = d.get("items") or []
    base = {"stage": "llm", "reason": str(d.get("reason", "")), "evidence": str(d.get("evidence", "")),
            "provider": res.provider or provider, "model": res.model or model, "raw": res.raw or ""}
    if _is_structured(d):
        return {**base, **_route_structured(d, focus_map)}
    # ---- 兼容分支：旧格式输出，原样保留阈值逻辑 ----
    if not d.get("actionable") or not items:
        return {**base, "verdict": "ignored", "confidence": 1.0, "item": None, "routed": None,
                "route": _route_info("ignored", "legacy:not_actionable", "旧格式输出：模型判定不需要处理")}
    conf = min(float(i.get("confidence", 0)) for i in items)
    thr = m2_settings(db)["confidence_threshold"]
    verdict = "todo" if conf >= thr else "review"
    why = (f"旧格式输出（没有 kind/audience_type），按置信度 {conf:.2f} "
           f"{'≥' if verdict == 'todo' else '<'} 阈值 {thr:.2f}")
    return {**base, "verdict": verdict, "confidence": conf, "item": items[0], "routed": None,
            "route": _route_info(verdict, "legacy:threshold", why)}


def _apply_user_rules(db: Any, block: dict, infos: list[dict], r: dict) -> dict:
    """模型感知之后、落库之前套用户规则；没有命中原样返回 r。规则层出错按未命中处理。"""
    from app import user_rules

    try:
        return user_rules.apply(db, block, infos, r)
    except Exception as exc:
        log.warning("用户规则层异常，按未命中处理：%s", type(exc).__name__)
        return r


def _suggestion_json(r: dict, item: dict | None = None, route: dict | None = None) -> str | None:
    """把判定时模型已给出的事项存成建议；不是模型判定或没有 item → None。

    item 缺省取 r["item"]（旧格式：items[0]）；新格式逐事项传入 item 与它的路由结果。
    """
    if item is None:
        item = r.get("item")
    if r.get("stage") != "llm" or not isinstance(item, dict):
        return None
    try:
        conf = float(item.get("confidence", r.get("confidence", 0)) or 0)
    except (TypeError, ValueError):
        conf = float(r.get("confidence") or 0)
    due = item.get("due_at")
    data: dict[str, Any] = {
        "title": str(item.get("title") or ""),
        "detail": str(item.get("detail") or ""),
        "due_at": str(due) if due not in (None, "") else None,
        "confidence": conf,
        "reason": str(r.get("reason") or ""),
    }
    if route is not None:  # 新格式才带，旧格式建议结构不变
        data["audience_type"] = route.get("audience_type", "")
        data["audience_text"] = route.get("audience_text", "")
        data["route_by"] = route.get("by", "")
        data["route_why"] = route.get("why", "")
    return json.dumps(data, ensure_ascii=False)


def _parse_suggestion(raw: Any) -> dict | None:
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _create_todo(s: Any, *, title: str, detail: str, due_at: Any, confidence: float,
                 block: dict, infos: list[dict], now: str, origin: str = "message") -> int:
    tid = _exec(s, "INSERT INTO todo_items(title,detail,due_at,status,confidence,created_at,updated_at,origin) "
                   "VALUES(:t,:d,:due,'open',:c,:n,:n,:o)",
                t=title, d=detail or "", due=due_at, c=confidence, n=now, o=origin)
    for i in infos:
        _exec(s, "INSERT INTO todo_sources(todo_id,msg_id,block_id,group_id,group_name,sender_name,quote,msg_time) "
                 "VALUES(:t,:m,:b,:g,:gn,:sn,:q,:mt)",
              t=tid, m=i["msg_id"], b=block["id"], g=i["group_id"], gn=i["group_name"],
              sn=i["sender_name"], q=i["text"], mt=i["time"])
    return tid


def _ask_once(s: Any, *, block_id: int, review_id: int, msg_id: Any, question: str, now: str) -> None:
    """同一消息块只在对话里问一次。"""
    asked = s.execute(sql("SELECT 1 FROM chat_messages c JOIN review_items ri ON ri.id=c.ref_review_id "
                          "WHERE c.kind='question' AND ri.block_id=:b"), {"b": block_id}).first()
    if not asked:
        _exec(s, "INSERT INTO chat_messages(role,kind,text,ref_review_id,ref_msg_id,created_at) "
                 "VALUES('assistant','question',:t,:r,:m,:n)", t=question, r=review_id, m=msg_id, n=now)


def _apply_routed(s: Any, *, block: dict, infos: list[dict], first_id: Any, r: dict,
                  now: str, events: list[dict], count_focus: bool = True) -> None:
    """新格式落库：todo 档事项各建一条待办，review 档事项各建一条待确认，ignored 档不落。"""
    snippet = block["text"].replace("\n", " ")[:40]
    focused: set[str] = set()
    for item, rt in r["routed"]:
        title = str(item.get("title") or block["text"][:30])
        conf = _item_conf(item, float(r.get("confidence") or 0))
        if rt["verdict"] == "todo":
            tid = _create_todo(s, title=title, detail=str(item.get("detail") or ""),
                               due_at=item.get("due_at") or None, confidence=conf,
                               block=block, infos=infos, now=now, origin="message")
            events.append({"type": "todo.created", "todo_id": tid, "msg_id": first_id, "title": title})
        elif rt["verdict"] == "review":
            if rt.get("focus"):
                question = (f"「{snippet}」这条跟你关注的「{rt['focus']}」有关，要建待办吗？"
                            f"请在待确认里选择确认或忽略。")
                focused.add(rt["focus"])
            elif _missing_due(rt):
                question = (f"「{snippet}」要求做「{title}」，但没说什么时候，需要你补个时间。"
                            f"请在待确认里选择确认或忽略。")
            else:
                question = f"「{snippet}」里的「{title}」：{rt['why']}。要建待办吗？请在待确认里选择确认或忽略。"
            rid = _exec(s, "INSERT INTO review_items(block_id,msg_id,confidence,question,evidence,status,"
                           "created_at,suggestion) VALUES(:b,:m,:c,:q,:e,'pending',:n,:sg)",
                        b=block["id"], m=first_id, c=conf, q=question, e=r["evidence"], n=now,
                        sg=_suggestion_json(r, item=item, route=rt))
            events.append({"type": "review.created", "review_id": rid, "msg_id": first_id,
                           "confidence": conf, "question": question})
            _ask_once(s, block_id=block["id"], review_id=rid, msg_id=first_id, question=question, now=now)
    if focused and count_focus:
        from app import focus_topics as ft

        ft.bump_hits(s, focused)


def _land(s: Any, *, block: dict, infos: list[dict], first_id: Any, r: dict, now: str,
          events: list[dict]) -> None:
    """按 r 落库：逐事项 / 待确认 / 待办。process_block 与命中恢复共用。"""
    if r.get("routed") is not None:
        _apply_routed(s, block=block, infos=infos, first_id=first_id, r=r, now=now, events=events)
    elif r["verdict"] == "review":
        if r.get("rule_why"):
            why = r["rule_why"]
        else:
            why = ("模型还没配置，我没法判断" if r["stage"] == "fallback"
                   else (r["reason"] or "信息不完整"))
        snippet = block["text"].replace("\n", " ")[:40]
        question = f"「{snippet}」{why}，要建待办吗？请在待确认里选择确认或忽略。"
        rid = _exec(s, "INSERT INTO review_items(block_id,msg_id,confidence,question,evidence,status,"
                       "created_at,suggestion) VALUES(:b,:m,:c,:q,:e,'pending',:n,:sg)",
                    b=block["id"], m=first_id, c=r["confidence"], q=question, e=r["evidence"], n=now,
                    sg=_suggestion_json(r))
        events.append({"type": "review.created", "review_id": rid, "msg_id": first_id,
                       "confidence": r["confidence"], "question": question})
        _ask_once(s, block_id=block["id"], review_id=rid, msg_id=first_id, question=question, now=now)
    elif r["verdict"] == "todo":
        item = r["item"] or {}
        title = str(item.get("title") or block["text"][:30])
        new_todo_id = _create_todo(s, title=title, detail=str(item.get("detail", "")),
                                   due_at=item.get("due_at"), confidence=r["confidence"],
                                   block=block, infos=infos, now=now, origin="message")
        events.append({"type": "todo.created", "todo_id": new_todo_id, "msg_id": first_id, "title": title})


async def process_block(db: Any, ctx: Any, block: dict) -> None:
    msg_ids = json.loads(block["msg_ids"])
    first_id = msg_ids[0] if msg_ids else None
    if _rows(db, "SELECT id FROM decisions WHERE block_id=:b", b=block["id"]):
        with db.write() as s:
            _exec(s, "UPDATE message_blocks SET processed_at=:n WHERE id=:b", n=now_iso(db), b=block["id"])
        return
    infos = msg_infos(db, msg_ids)
    reason = rule_ignore(block["text"])
    if reason:
        r = {"stage": "rule", "verdict": "ignored", "reason": reason, "confidence": 1.0,
             "evidence": block["text"][:200], "provider": "", "model": "", "raw": "", "item": None,
             "route": _route_info("ignored", "rule", f"规则粗筛：{reason}"), "routed": None}
    else:
        r = await _judge(db, ctx, block, infos)
        if not r["evidence"]:
            r["evidence"] = block["text"][:200]
        r = _guard_role_me(db, r)
        r = _apply_user_rules(db, block, infos, r)  # 只剩「不看」，只调一次
    route = r.get("route")
    route_json = json.dumps(route, ensure_ascii=False) if route else None
    log.info("决策 block=%s stage=%s verdict=%s by=%s", block["id"], r["stage"], r["verdict"],
             (route or {}).get("by", ""))
    now = now_iso(db)
    events: list[dict] = []
    with db.write() as s:
        _exec(s, "INSERT INTO decisions(block_id,msg_id,stage,verdict,reason,confidence,evidence,provider,model,"
                 "raw_response,created_at,route) VALUES(:b,:m,:st,:v,:r,:c,:e,:p,:mo,:raw,:n,:rt)",
              b=block["id"], m=first_id, st=r["stage"], v=r["verdict"], r=r["reason"], c=r["confidence"],
              e=r["evidence"], p=r["provider"], mo=r["model"], raw=r["raw"][:8000], n=now, rt=route_json)
        if r.get("user_rule_id"):
            _exec(s, "UPDATE user_rules SET hits=hits+1, last_hit_at=:n WHERE id=:i",
                  n=now, i=r["user_rule_id"])
        hit = r.get("rule_hit")
        if hit:  # 命中记录
            _exec(s, "INSERT INTO rule_hits(rule_id,msg_id,block_id,verdict_before,restored,snapshot,created_at) "
                     "VALUES(:ri,:m,:b,:v,0,:sn,:n)",
                  ri=hit["rule_id"], m=first_id, b=block["id"], v=hit["verdict_before"],
                  sn=json.dumps(hit["snapshot"], ensure_ascii=False, default=str), n=now)
        _land(s, block=block, infos=infos, first_id=first_id, r=r, now=now, events=events)
        _exec(s, "UPDATE message_blocks SET processed_at=:n WHERE id=:b", n=now, b=block["id"])
    for ev in events:
        hub.publish(ev)


def _rejudge_split(r: dict) -> tuple[list[tuple[dict, dict]], bool]:
    """→ (该进待办的事项, 是否还有该待确认的事项)。"""
    routed = r.get("routed")
    if routed is not None:
        todo = [(it or {}, rt) for it, rt in routed if rt.get("verdict") == "todo"]
        return todo, any(rt.get("verdict") == "review" for _it, rt in routed)
    v = r.get("verdict")
    if v == "todo":
        return [(r.get("item") or {}, {})], False
    return [], v == "review"





def _flush_lock_for(ctx: Any = None) -> asyncio.Lock:
    """判定互斥锁：**每个应用一份**。

    模块级 asyncio.Lock 会绑定"第一次 await 它的那个事件循环"，
    每个 app/loop 都会新建自己的一套，复用同一个锁会抛
    "Lock object is bound to a different event loop"。挂在 ctx 上最省事。
    """
    lock = getattr(ctx, "flush_lock", None) if ctx is not None else None
    if lock is None:
        lock = asyncio.Lock()
        if ctx is not None:
            ctx.flush_lock = lock
    return lock


async def flush(db: Any, ctx: Any) -> int:
    async with _flush_lock_for(ctx):
        return await _flush_once(db, ctx)


async def _flush_once(db: Any, ctx: Any) -> int:
    build_blocks(db)
    if not m2_settings(db)["pipeline_enabled"]:
        return 0
    n = 0
    for b in _rows(db, "SELECT * FROM message_blocks WHERE processed_at IS NULL ORDER BY id"):
        await process_block(db, ctx, b)
        n += 1
    return n


# ------------------------------------------------------------------ 序列化
def _todo_out(db: Any, t: dict, policy: Any = None) -> dict:
    src = _rows(db, "SELECT msg_id, group_id, group_name, sender_name, quote, msg_time FROM todo_sources "
                    "WHERE todo_id=:t ORDER BY msg_time, id", t=t["id"])
    keys = ("id", "title", "detail", "due_at", "status", "confidence", "created_at", "updated_at", "origin")
    if policy is None:
        policy = _expiry_policy(db)
    return {**{k: t[k] for k in keys}, "sources": src, "expired": policy.is_expired(t)}


def _get_todo(db: Any, tid: int) -> dict:
    rows = _rows(db, "SELECT * FROM todo_items WHERE id=:i", i=tid)
    if not rows:
        raise _bad("待办不存在", 404)
    return rows[0]




_TODO_CLOSED = ("done", "dismissed", "deleted", "cancelled")  # ← 按 todo_items.status 实际枚举核对


def _fmt_due(due: str) -> str:
    try:
        dt = datetime.fromisoformat(due)
    except ValueError:
        return due
    s = f"{dt.month}月{dt.day}日"
    return s if (dt.hour, dt.minute) == (23, 59) else f"{s} {dt:%H:%M}"


def _chat_event_duplicate(s: Any, chk: Any) -> tuple[bool, dict | None]:
    """同名（且有日期时同日）的未完成待办，或同名的对话待确认 → (已经有了, 回读到的行)。"""
    from app import event_route

    marks = ",".join(f"'{x}'" for x in _TODO_CLOSED)
    for row in s.execute(sql("SELECT id, title, due_at, status FROM todo_items "
                             f"WHERE COALESCE(status,'') NOT IN ({marks})")).all():
        if not event_route.same_item(chk.title, str(row[1] or "")):
            continue
        if chk.due_at and row[2] and str(row[2])[:10] != chk.due_at[:10]:
            continue
        return True, {"id": int(row[0]), "title": str(row[1] or ""), "due_at": row[2], "status": row[3]}
    for row in s.execute(sql("SELECT id, question, dedupe_key, status FROM review_items WHERE status='pending' "
                             "AND source='chat' AND dedupe_key IS NOT NULL")).all():
        if event_route.same_item(chk.title, str(row[2] or "")):
            return True, {"id": int(row[0]), "question": str(row[1] or ""),
                          "dedupe_key": str(row[2] or ""), "status": row[3]}
    return False, None


def _chat_review_question(chk: Any) -> str:
    when = f"（你说的是「{chk.time_text}」）" if chk.time_text else ""
    if chk.ask_reason == "no_request":
        return f"你提到「{chk.title}」，{_fmt_due(chk.due_at or '')}。要记成待办吗？"
    if chk.ask_reason == "no_date":
        return f"「{chk.title}」具体哪天？{when}定下日期才能记成待办。"
    return f"你提到「{chk.title}」{when}，要记成待办吗？哪天？"


_CHAT_REVIEW_LINES = {
    "no_request": "你没说要记，要不要记你来定",
    "no_date": "日期还没定",
    "both": "你没说要记，日期也还没定",
}


RAW_REVIEW_TITLE_LEN = 30


def land_chat_event(db: Any, spec: dict, user_text: str, *,
                    chat_message_id: int) -> tuple[str, str, dict | None, str]:
    """对话事件 → 四道核对 → 路由落地。返回 (kind, 结果行, 回读行, 失败原因)；
    kind∈todo/review/dup/drop。标记来的和抽取来的事件都走这里。"""
    from app import event_route

    now_s = now_iso(db)
    chk = event_route.verify_event(spec if isinstance(spec, dict) else {}, user_text,
                                   datetime.fromisoformat(now_s))
    log.info("对话事件核对：route=%s reason=%s grain=%s notes=%s",
             chk.route, chk.ask_reason, chk.grain, ";".join(chk.notes) or "-")
    if chk.route == "drop":
        return "drop", "", None, "、".join(chk.notes) or "没能落下"
    return _land_checked(db, chk, user_text, chat_message_id=chat_message_id, now_s=now_s)


def land_chat_raw_review(db: Any, user_text: str, *,
                         chat_message_id: int) -> tuple[str, str, dict | None, str]:
    """ 退路：G1 真但抽取没给出事件 → 原话整句进待确认。绝不建待办。"""
    from app import event_route

    flat = re.sub(r"\s+", " ", user_text).strip()
    title = flat[:RAW_REVIEW_TITLE_LEN] + ("…" if len(flat) > RAW_REVIEW_TITLE_LEN else "")
    chk = event_route.EventCheck(
        "review", title=title, asked=True, grain=event_route.program_grain(user_text),
        time_text="", due_at=None, ask_reason="no_request",
        dedupe_key=event_route.norm_title(user_text), notes=["抽取未给出事件，原话进待确认"])
    return _land_checked(db, chk, user_text, chat_message_id=chat_message_id, now_s=now_iso(db),
                         question=f"你说过「{title}」，要记成待办吗？补上日期就能建。",
                         line=f"「{title}」没能自动整理成待办，原话放进了待确认")


def _land_checked(db: Any, chk: Any, user_text: str, *, chat_message_id: int, now_s: str,
                  question: str | None = None,
                  line: str | None = None) -> tuple[str, str, dict | None, str]:
    """已核对的 EventCheck → 去重 → todo / review。结果行只按实际落地拼；row 是写后回读。"""
    events: list[dict] = []
    tid: int | None = None
    rid: int | None = None
    dup_row: dict | None = None
    with db.write() as s:
        dup, dup_row = _chat_event_duplicate(s, chk)
        if dup:
            kind = "dup"
        elif chk.route == "todo":
            tid = _create_todo(s, title=chk.title, detail=f"来自对话：{user_text[:200]}",
                               due_at=chk.due_at, confidence=1.0, block={"id": None}, infos=[],
                               now=now_s, origin="chat")
            events.append({"type": "todo.created", "todo_id": tid, "msg_id": None, "title": chk.title})
            kind = "todo"
        else:
            q = question or _chat_review_question(chk)
            sg = json.dumps({"from": "chat", "title": chk.title, "time_text": chk.time_text,
                             "time_grain": chk.grain, "due_at": chk.due_at}, ensure_ascii=False)
            rid = _exec(s, "INSERT INTO review_items(block_id,msg_id,confidence,question,evidence,status,"
                           "created_at,suggestion,source,chat_message_id,ask_reason,time_text,dedupe_key) "
                           "VALUES(0,NULL,0.5,:q,:e,'pending',:n,:sg,'chat',:cm,:ar,:tt,:dk)",
                        q=q, e=user_text[:500], n=now_s, sg=sg, cm=chat_message_id,
                        ar=chk.ask_reason, tt=chk.time_text, dk=chk.dedupe_key)
            events.append({"type": "review.created", "review_id": rid, "msg_id": None,
                           "confidence": 0.5, "question": q})
            kind = "review"
    for e in events:
        hub.publish(e)
    if kind == "dup":
        if dup_row is not None:
            dup_row.setdefault("title", chk.title)
        return kind, f"「{chk.title}」已经有了", dup_row, ""
    if kind == "todo":
        rows = _rows(db, "SELECT id, title, due_at, status, created_at FROM todo_items WHERE id=:i", i=tid)
        row = rows[0] if rows else None
        return kind, f"已添加待办：{chk.title} · {_fmt_due(chk.due_at or '')}（可改）", row, ""
    rows = _rows(db, "SELECT id, question, status, source, ask_reason, dedupe_key "
                     "FROM review_items WHERE id=:i", i=rid)
    row = rows[0] if rows else None
    if row is not None:
        row["title"] = chk.title  # 回执行要显示待确认事项的标题（库里只有 question）
    text = line or f"「{chk.title}」放进了待确认：{_CHAT_REVIEW_LINES[chk.ask_reason or 'both']}"
    return kind, text, row, ""


def _profile_one_off_hits(db: Any, profile_raw: Any) -> list[str]:
    from app import event_route

    after = _normalize_about(str(profile_raw.get("about") or "")) if isinstance(profile_raw, dict) else ""
    before = str(_profile(db).get(PROFILE_ABOUT_KEY) or "")
    return event_route.one_off_hits(before, after)


def _profile_gate_why(hits: list[str], event_kind: str) -> str:
    """闸门拒绝的原因（回执用）；_profile_gate_line 是旧的整体结果行。"""
    shown = "、".join(f"「{h}」" for h in list(dict.fromkeys(hits))[:3])
    tail = {"review": "，日期相关的事我放进了待确认",
            "todo": "，日期相关的事已经记成待办",
            "dup": "，日期相关的事之前已经记过"}.get(event_kind, "，一次性的日期不写进「关于我」")
    return f"里面带了具体日期（{shown}）{tail}"


def _profile_gate_line(hits: list[str], event_kind: str) -> str:
    return "「关于我」这次没改：" + _profile_gate_why(hits, event_kind)


def _sample(s: Any, *, source: str, group_id: Any, sender: str, text: str, verdict: str,
            note: str, now: str) -> None:
    _exec(s, "INSERT INTO memory_samples(source,group_id,sender_name,text,verdict,note,created_at) "
             "VALUES(:so,:g,:se,:t,:v,:no,:n)", so=source, g=group_id, se=sender, t=text, v=verdict,
          no=note, n=now)


def _status_conclusion(old: str, new: str) -> tuple[str, str]:
    """待办状态变化 → (样本 verdict, 样本结论)。"""
    if new == "dropped":
        return "ignored", "用户认为不该建待办"
    if new == "done":
        return "todo", "用户已完成"
    if old == "dropped":
        return "todo", "用户认为该做"
    return "todo", "用户认为还没做完"


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise _bad("请求体不是合法的 JSON") from None
    if not isinstance(body, dict):
        raise _bad("请求体必须是 JSON 对象")
    return body


# ------------------------------------------------------------------ 路由
router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/settings")
def get_settings(request: Request) -> dict:
    from app.api import routes_settings

    ctx, db = _ctx_db(request)
    return {**_dump(routes_settings.get_settings(ctx=ctx, db=db)), **m2_settings(db)}


@router.put("/settings")
async def put_settings(request: Request) -> dict:
    from app.api import routes_settings
    from app.schemas import SettingsUpdate

    ctx, db = _ctx_db(request)
    body = await _json(request)
    m2_values = validate_m2_settings(body)  # 先校验，任一越界整次不写
    m1 = _dump(routes_settings.put_settings(SettingsUpdate(**body), ctx=ctx, db=db))
    save_m2_settings(db, m2_values)
    return {**m1, **m2_settings(db)}


@router.get("/llm/selection")
def llm_selection_get(request: Request) -> dict:
    """判定用哪个、对话用哪个（null = 跟随默认）。"""
    _, db = _ctx_db(request)
    sel = llm_profiles.selection(db)
    act = llm_profiles.active(db)
    names = {p["id"]: p["name"] for p in llm_profiles.list_all(db)}
    return {
        "judge": sel.get("judge"),
        "chat": sel.get("chat"),
        "judge_name": names.get(sel.get("judge") or -1, ""),
        "chat_name": names.get(sel.get("chat") or -1, ""),
        "fallback_id": (act or {}).get("id"),
        "fallback_name": (act or {}).get("name", ""),
    }


@router.put("/llm/selection")
async def llm_selection_put(request: Request) -> dict:
    """只传要改的那一项；传 null 表示恢复默认。"""
    _, db = _ctx_db(request)
    body = await _json(request)
    return llm_profiles.set_selection(db, body)


@router.get("/llm/profiles")
def llm_profiles_list(request: Request) -> list:
    """已保存的模型档案（密钥只回 api_key_set 布尔）。"""
    _, db = _ctx_db(request)
    return llm_profiles.list_all(db)


@router.post("/llm/profiles")
async def llm_profiles_create(request: Request) -> Any:
    _, db = _ctx_db(request)
    body = await _json(request)
    return JSONResponse(llm_profiles.create(db, body), status_code=201)


@router.patch("/llm/profiles/{pid}")
async def llm_profiles_update(pid: int, request: Request) -> Any:
    _, db = _ctx_db(request)
    body = await _json(request)
    out = llm_profiles.update(db, pid, body)
    if out is None:
        raise _bad("模型档案不存在", 404)
    return out


@router.delete("/llm/profiles/{pid}")
def llm_profiles_delete(pid: int, request: Request) -> dict:
    _, db = _ctx_db(request)
    if not llm_profiles.delete(db, pid):
        raise _bad("模型档案不存在", 404)
    return {"ok": True, "id": pid}


@router.post("/llm/profiles/{pid}/activate")
def llm_profiles_activate(pid: int, request: Request) -> Any:
    _, db = _ctx_db(request)
    out = llm_profiles.activate(db, pid)
    if out is None:
        raise _bad("模型档案不存在", 404)
    return out


@router.post("/llm/profiles/{pid}/test")
async def llm_profiles_test(pid: int, request: Request) -> dict:
    """用这条档案自己的配置测一次连通性（不动其它档案，也不改当前生效项）。"""
    _, db = _ctx_db(request)
    row = llm_profiles.get(db, pid)
    if row is None:
        raise _bad("模型档案不存在", 404)
    from app.llm.upstream import judge_with_fallback

    started = time.time()
    res = await judge_with_fallback(config=llm_profiles.config_of(row), text="连通性测试：收到请回复",
                                    timeout=15.0, surface_message=True,
                                    thinking=bool(m2_settings(db)["judge_thinking"]))
    latency = int((time.time() - started) * 1000)
    ok = bool(getattr(res, "ok", False))
    err = "" if ok else str(getattr(res, "error", "") or "失败")
    llm_profiles.mark_test(db, pid, ok=ok, error=err)
    return {"ok": ok, "latency_ms": latency, "error": err or None,
            "provider": row.get("provider"), "model": row.get("model")}


@router.get("/usage/summary")
def usage_summary(request: Request, days: int = 30) -> dict:
    """调用账本汇总。days<=0 表示全部历史。"""
    _, db = _ctx_db(request)
    return ledger.summary(db, days=days)


@router.get("/usage/recent")
def usage_recent(request: Request, limit: int = 50) -> list:
    _, db = _ctx_db(request)
    return ledger.recent(db, limit=limit)


@router.get("/stats")
def get_stats(request: Request) -> dict:
    from app.api import routes_stats

    ctx, db = _ctx_db(request)
    base = _dump(routes_stats.stats(ctx=ctx, db=db))
    count = lambda q: _rows(db, q)[0]["n"]  # noqa: E731
    return {**base, "llm_calls": db.get_counter(LLM_COUNTER),
            "todos_open": count("SELECT COUNT(*) n FROM todo_items WHERE status='open'"),
            "review_pending": count("SELECT COUNT(*) n FROM review_items WHERE status='pending'")}


@router.post("/settings/llm/test")
async def llm_test(request: Request) -> dict:
    ctx, db = _ctx_db(request)
    cfg, provider, model = llm_config(db, ctx, "judge")
    out = {"ok": False, "provider": provider, "model": model, "latency_ms": 0}
    if cfg is None or not getattr(cfg, "configured", False):
        return {**out, "error": "not_configured"}
    from app.llm.upstream import judge_with_fallback

    t0 = time.monotonic()
    res = await judge_with_fallback(config=cfg, text="连通性测试：收到请回复", timeout=15.0,
                                    surface_message=True)
    db.incr_counter(LLM_COUNTER, 1)
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return {**out, "ok": True} if res.ok else {**out, "error": res.error or "llm_error"}


@router.post("/pipeline/flush")
async def pipeline_flush(request: Request) -> dict:
    ctx, db = _ctx_db(request)
    return {"processed": await flush(db, ctx)}


@router.get("/pipeline/status")
def pipeline_status(request: Request) -> dict:
    ctx, db = _ctx_db(request)
    s = m2_settings(db)
    _, provider, model = llm_config(db, ctx, "judge")
    pending = _rows(db, "SELECT COUNT(*) n FROM message_blocks WHERE processed_at IS NULL")[0]["n"]
    return {"enabled": s["pipeline_enabled"], "provider": provider, "model": model, "pending": pending,
            "llm_calls": db.get_counter(LLM_COUNTER),
            "last_error": _state(db).get("last_error")}


DEC_COLS = "id, block_id, msg_id, stage, verdict, reason, confidence, evidence, provider, model, created_at, route"


def _dec_out(row: dict) -> dict:
    """决策行对外输出：route 列（JSON 文本）解析成对象； 之前的旧行为 None。"""
    raw = row.get("route")
    try:
        v = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        v = None
    return {**row, "route": v if isinstance(v, dict) else None}


def _block_of_msg(db: Any, msg_id: int) -> dict | None:
    for b in _rows(db, "SELECT * FROM message_blocks WHERE msg_ids LIKE :p ORDER BY id DESC", p=f"%{msg_id}%"):
        if msg_id in json.loads(b["msg_ids"]):
            return b
    return None


def _latest_verdict(db: Any, block_id: int) -> str | None:
    """块的最新结论：按 decisions.id 倒序取第一条（AI 与人工决策共存时，后写入者为准）。"""
    d = _rows(db, "SELECT verdict FROM decisions WHERE block_id=:b ORDER BY id DESC LIMIT 1", b=block_id)
    return d[0]["verdict"] if d else None


@router.get("/messages/groups")
def message_groups(request: Request) -> list:
    """只列出"确实有消息"的群（含私聊 sender）。

    消息页的群筛选器必须用它，而不是 /api/groups——后者是白名单选择器用的
    全量群列表，包含大量从未收过消息的群，选中它们只会得到空列表。
    """
    _, db = _ctx_db(request)
    rows = _rows(db, "SELECT m.group_id AS group_id, COALESCE(g.group_name, '') AS group_name, "
                     "COUNT(*) AS message_count, MAX(m.time) AS last_time "
                     "FROM messages m LEFT JOIN groups g ON g.group_id = m.group_id "
                     "GROUP BY m.group_id ORDER BY message_count DESC, group_id")
    out = []
    for r in rows:
        out.append({
            "group_id": r["group_id"],
            "group_name": r["group_name"] or ("私聊" if r["group_id"] is None else f"群 {r['group_id']}"),
            "message_count": int(r["message_count"] or 0),
            "last_time": r["last_time"],
        })
    return out


def _apply_manual_verdict(db: Any, msg_row: dict, msg_id: int, verdict: str, *,
                          title: str, detail: str, due_at: str | None) -> dict[str, Any]:
    """人工改判的全部落库动作（调用方须持有 _flush_lock）。只追加/改状态，不删除任何行。"""
    build_blocks(db)  # 消息若还没成块，先成块，避免之后管线再判一次
    block = _block_of_msg(db, msg_id)
    infos = msg_infos(db, [msg_id])
    info = infos[0] if infos else {
        "msg_id": msg_id, "group_id": msg_row.get("group_id"), "group_name": "",
        "sender_name": str(_pick(msg_row, "sender_name", "card", "nickname")),
        "text": str(_pick(msg_row, "text", "content", "raw_message")),
        "time": int(_pick(msg_row, "time", default=0) or 0),
    }
    text = info["text"]
    prev = _latest_verdict(db, block["id"]) if block is not None else None
    prev_s = prev or "无"
    linked = _rows(db, "SELECT id, status FROM todo_items WHERE id IN "
                       "(SELECT todo_id FROM todo_sources WHERE msg_id=:m) ORDER BY id DESC", m=msg_id)
    now = now_iso(db)
    todo_id: int | None = None
    created = False
    final_title = ""
    dropped: list[int] = []
    route_json = json.dumps(_route_info(verdict, "manual", f"人工改判（原判：{prev_s}）"), ensure_ascii=False)
    with db.write() as s:
        if block is None:  # 管线关闭等情况下消息没有块：补一个已处理的单消息块，用来挂决策
            bid = _exec(s, "INSERT INTO message_blocks(group_id,sender_id,msg_ids,text,msg_count,created_at,"
                           "processed_at) VALUES(:g,:u,:m,:t,1,:n,:n)",
                        g=info["group_id"], u=int(_pick(msg_row, "user_id", "sender_id", default=0) or 0),
                        m=json.dumps([msg_id]), t=text, n=now)
            block = {"id": bid}
        else:  # 块还在排队：人工决定已给出，管线不必再花 token 判它
            _exec(s, "UPDATE message_blocks SET processed_at=:n WHERE id=:b AND processed_at IS NULL",
                  n=now, b=block["id"])
        if verdict == "todo":
            reuse = (next((t for t in linked if t["status"] == "open"), None)
                     or next((t for t in linked if t["status"] == "dropped"), None))
            if reuse is not None:  # 已有（含被忽略的）待办：恢复它，来源不变，不重复建
                todo_id = int(reuse["id"])
                sets: dict[str, Any] = {"status": "open"}
                if title:
                    sets["title"] = title
                if detail:
                    sets["detail"] = detail
                if due_at:
                    sets["due_at"] = due_at
                assign = ", ".join(f"{k}=:{k}" for k in sets)
                _exec(s, f"UPDATE todo_items SET {assign}, updated_at=:now WHERE id=:id",
                      **sets, now=now, id=todo_id)
            else:
                final_title = title or text.replace("\n", " ")[:30] or "（无正文）"
                todo_id = _create_todo(s, title=final_title, detail=detail, due_at=due_at or None,
                                       confidence=1.0, block=block, infos=[info], now=now, origin="manual")
                created = True
            review_status = "confirmed"
        else:
            for t in linked:
                if t["status"] == "open":
                    _exec(s, "UPDATE todo_items SET status='dropped', updated_at=:n WHERE id=:i",
                          n=now, i=t["id"])
                    dropped.append(int(t["id"]))
            review_status = "ignored"
        # 该块若还挂着待确认，一并关掉，免得列表里残留已由人决定的条目
        _exec(s, "UPDATE review_items SET status=:st, resolved_at=:n WHERE block_id=:b AND status='pending'",
              st=review_status, n=now, b=block["id"])
        _exec(s, "INSERT INTO decisions(block_id,msg_id,stage,verdict,reason,confidence,evidence,provider,model,"
                 "raw_response,created_at,route) VALUES(:b,:m,'manual',:v,:r,1.0,:e,'','','',:n,:rt)",
              b=block["id"], m=msg_id, v=verdict, r=f"人工改判（原判：{prev_s}）", e=_clip(text), n=now,
              rt=route_json)
        _exec(s, "INSERT INTO feedback(action,todo_id,msg_id,note,created_at) VALUES('edit',:t,:m,:no,:n)",
              t=todo_id if todo_id is not None else (dropped[0] if dropped else None), m=msg_id,
              no=f"人工改判：{prev_s} → {verdict}", n=now)
        conclusion = "用户认为该做" if verdict == "todo" else "用户认为不该做"
        _sample(s, source="manual", group_id=info["group_id"], sender=info["sender_name"], text=_clip(text),
                verdict=verdict, note=f"{conclusion}（原判：{prev_s}）", now=now)
    return {"todo_id": todo_id, "created": created, "title": final_title, "dropped": dropped}


@router.post("/messages/{msg_id}/verdict")
async def manual_verdict(msg_id: int, request: Request) -> dict:
    """人工改判：只接受 todo / ignored；不调用模型；追加 stage=manual 决策，不覆盖历史。"""
    _, db = _ctx_db(request)
    body = await _json(request)
    verdict = body.get("verdict")
    if verdict not in MANUAL_VERDICTS:
        raise _bad("改判失败：verdict 只能是 todo 或 ignored")
    fields: dict[str, Any] = {}
    for name in ("title", "detail", "due_at"):
        v = body.get(name)
        if v is not None and not isinstance(v, str):
            raise _bad(f"改判失败：{name} 必须是字符串")
        fields[name] = (v or "").strip()
    rows = _rows(db, "SELECT * FROM messages WHERE msg_id=:m ORDER BY rowid LIMIT 1", m=msg_id)
    if not rows:
        raise _bad("消息不存在", 404)
    async with _flush_lock_for(getattr(request.app.state, "ctx", None)):  # 与管线互斥：避免 AI 判定盖过人工决定
        res = _apply_manual_verdict(db, rows[0], msg_id, verdict, title=fields["title"],
                                    detail=fields["detail"], due_at=fields["due_at"] or None)
    if res["created"]:
        hub.publish({"type": "todo.created", "todo_id": res["todo_id"], "msg_id": msg_id, "title": res["title"]})
    return {"ok": True, "msg_id": msg_id, "verdict": verdict, "todo_id": res["todo_id"],
            "dropped_todo_ids": res["dropped"]}


# ------------------------------------------------------------------  要点记忆
@router.get("/agent/memory")
def agent_memory_list(request: Request) -> list:
    from app import agent_memory

    _, db = _ctx_db(request)
    return agent_memory.list_all(db)


@router.post("/agent/memory")
async def agent_memory_create(request: Request) -> Any:
    from app import agent_memory, user_rules

    _, db = _ctx_db(request)
    body = await _json(request)
    kind = body.get("kind")
    content = body.get("content")
    if kind not in agent_memory.KINDS:
        raise _bad("保存记忆失败：kind 只能是 preference / fact / rule")
    if not isinstance(content, str) or not content.strip():
        raise _bad("保存记忆失败：content 不能为空")
    if len(content) > 500:
        raise _bad("保存记忆失败：记忆应是一句要点（≤500 字输入，存储时截到 80 字）")
    rule_raw = body.get("rule")
    if rule_raw is None:  # 原有路径，一点不变
        item, merged = agent_memory.upsert(db, kind, content, source="manual")
        return JSONResponse(item | {"merged": merged}, status_code=200 if merged else 201)
    # 先校验规则，任何一处不合法整次不写（不会留下半条记忆）
    if kind != "rule":
        raise _bad("保存记忆失败：只有 kind=rule 的记忆可以带 rule（偏好/事实会被自动整理删除，不适合挂执行规则）")
    spec = user_rules.validate(rule_raw, what="保存记忆失败")
    item, merged, rule = agent_memory.upsert_with_rule(db, kind, content, spec, source="manual")
    return JSONResponse(item | {"merged": merged, "rule": rule}, status_code=200 if merged else 201)


@router.delete("/agent/memory/{memory_id}")
def agent_memory_delete(memory_id: int, request: Request) -> dict:
    from app import agent_memory

    _, db = _ctx_db(request)
    if not agent_memory.delete(db, memory_id):  # 连带删掉它带出的规则
        raise _bad("记忆不存在", 404)
    return {"ok": True, "id": memory_id}


@router.post("/agent/memory/slim")
def agent_memory_slim(request: Request) -> dict:
    from app import agent_memory

    _, db = _ctx_db(request)
    return agent_memory.slim(db)


# ------------------------------------------------------------------  用户规则
@router.get("/agent/rules")
def agent_rules_list(request: Request) -> list:
    from app import user_rules

    _, db = _ctx_db(request)
    return user_rules.list_all(db)


@router.post("/agent/rules")
async def agent_rules_create(request: Request) -> Any:
    from app import user_rules

    _, db = _ctx_db(request)
    body = await _json(request)
    spec = user_rules.validate(body)
    return JSONResponse(user_rules.create(db, spec, source="manual"), status_code=201)


@router.delete("/agent/rules/{rule_id}")
def agent_rules_delete(rule_id: int, request: Request) -> dict:
    from app import user_rules

    _, db = _ctx_db(request)
    if not user_rules.delete(db, rule_id):
        raise _bad("规则不存在", 404)
    return {"ok": True, "id": rule_id}


# ------------------------------------------------------------------  建议规则确认 / 拒绝
@router.post("/agent/rules/{rule_id}/confirm")
def agent_rules_confirm(rule_id: int, request: Request) -> dict:
    """suggested → active；已是 active 幂等成功；不存在 404。确认后下一次判定立即生效（判定每次现查表）。"""
    from app import user_rules

    _, db = _ctx_db(request)
    rule = user_rules.confirm(db, rule_id)
    if rule is None:
        raise _bad("规则不存在", 404)
    return {"ok": True, "id": rule_id, "status": rule.get("status", "active")}


@router.post("/agent/rules/{rule_id}/reject")
def agent_rules_reject(rule_id: int, request: Request) -> dict:
    """拒绝 = 删除（无论状态）；不存在 404。"""
    from app import user_rules

    _, db = _ctx_db(request)
    if not user_rules.reject(db, rule_id):
        raise _bad("规则不存在", 404)
    return {"ok": True, "id": rule_id}


@router.get("/decisions")
def list_decisions(request: Request, msg_id: int | None = None, verdict: str | None = None,
                   limit: int = 50) -> list:
    _, db = _ctx_db(request)
    where, p = ["1=1"], {"l": max(1, min(limit, 500))}
    if msg_id is not None:
        b = _block_of_msg(db, msg_id)
        if not b:
            return []
        where.append("block_id=:b")
        p["b"] = b["id"]
    if verdict:
        where.append("verdict=:v")
        p["v"] = verdict
    rows = _rows(db, f"SELECT {DEC_COLS} FROM decisions WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT :l", **p)
    return [_dec_out(r) for r in rows]


@router.get("/decisions/by-msg/{msg_id}")
def decision_by_msg(msg_id: int, request: Request) -> dict:
    _, db = _ctx_db(request)
    b = _block_of_msg(db, msg_id)
    d = _rows(db, f"SELECT {DEC_COLS} FROM decisions WHERE block_id=:b ORDER BY id DESC LIMIT 1",
              b=b["id"]) if b else []
    if not d:
        raise _bad("这条消息还没有决策（可能未处理或被闸门丢弃）", 404)
    return {"decision": _dec_out(d[0]),
            "block": {"id": b["id"], "msg_ids": json.loads(b["msg_ids"]), "text": b["text"]}}


# ---- 收起提示 / 保留期（必须排在 /todos/{todo_id} 之前）----


def _expired_summary(db: Any) -> dict:
    policy = _expiry_policy(db)
    rows = _rows(db, "SELECT * FROM todo_items WHERE status='open'")
    return lifecycle.expired_summary(rows, policy, runtime_config.get_raw(db, RETENTION_ACKED_AT_KEY))


@router.get("/todos/expired-summary")
def todos_expired_summary(request: Request) -> dict:
    _, db = _ctx_db(request)
    return _expired_summary(db)


@router.post("/todos/expired-summary/ack")
def todos_expired_ack(request: Request) -> dict:
    _, db = _ctx_db(request)
    acked = lifecycle.ack_value(_expired_summary(db))
    runtime_config.set_raw(db, RETENTION_ACKED_AT_KEY, acked)
    return {"ok": True, "acked_at": acked}


@router.post("/retention/run")
async def retention_run(request: Request) -> dict:
    """手动触发一轮清理（与每日后台任务同一实现；查删都在线程池、分批）。"""
    _, db = _ctx_db(request)
    return await lifecycle.run_all(db, trigger="manual")


@router.get("/retention/settings")
def get_retention_settings(request: Request) -> dict:
    _, db = _ctx_db(request)
    return lifecycle.retention_view(runtime_config.load_retention(db))


@router.put("/retention/settings")
async def put_retention_settings(request: Request) -> dict:
    _, db = _ctx_db(request)
    body = await _json(request)
    if not isinstance(body, dict):
        raise _bad("保存保留期设置失败：请求体必须是对象")
    try:
        await asyncio.to_thread(runtime_config.apply_retention_update, db, body)
    except ValueError as exc:
        raise _bad(f"保存保留期设置失败：{exc}") from None
    return lifecycle.retention_view(await asyncio.to_thread(runtime_config.load_retention, db))


@router.get("/todos")
def list_todos(request: Request, view: str | None = None, status: str | None = None,
               limit: int = 50, before_id: int | None = None) -> list:
    """view=main（默认）/ history；只传旧参数 status= 时走旧口径（兼容，含 status=all）。view 优先。"""
    _, db = _ctx_db(request)
    lim = max(1, min(limit, 200))
    policy = _expiry_policy(db)

    if view is None and status is not None:  # 旧口径：不做视图过滤
        rows = _rows(db, "SELECT * FROM todo_items WHERE (:s = 'all' OR status=:s) AND (:b IS NULL OR id < :b) "
                         "ORDER BY id DESC LIMIT :l", s=status, b=before_id, l=lim)
        return [_todo_out(db, t, policy) for t in rows]

    view = view or "main"
    if view == "main":
        rows = _rows(db, "SELECT * FROM todo_items WHERE status='open' AND (:b IS NULL OR id < :b)", b=before_id)
        picked = [t for t in rows if not policy.is_expired(t)]
        picked.sort(key=policy.main_sort_key)
    elif view == "history":
        rows = _rows(db, "SELECT * FROM todo_items WHERE status IN ('open','done','dropped') "
                         "AND (:b IS NULL OR id < :b)", b=before_id)
        picked = [t for t in rows if t["status"] != "open" or policy.is_expired(t)]
        picked.sort(key=policy.history_sort_key, reverse=True)
    else:
        raise _bad("查询待办失败：view 只能是 main / history")
    return [_todo_out(db, t, policy) for t in picked[:lim]]


@router.get("/todos/{todo_id}")
def get_todo(todo_id: int, request: Request) -> dict:
    _, db = _ctx_db(request)
    return _todo_out(db, _get_todo(db, todo_id))


@router.patch("/todos/{todo_id}")
async def patch_todo(todo_id: int, request: Request) -> dict:
    _, db = _ctx_db(request)
    body = await _json(request)
    old = _get_todo(db, todo_id)
    changes = {k: body[k] for k in ("title", "detail", "due_at", "status") if k in body}
    if "status" in changes and changes["status"] not in ("open", "done", "dropped"):
        raise _bad("修改待办失败：status 只能是 open / done / dropped")
    if "title" in changes and not str(changes["title"] or "").strip():
        raise _bad("修改待办失败：标题不能为空")
    if not changes:
        return _todo_out(db, old)
    now = now_iso(db)
    note = "；".join(f"{k}: {old[k]} → {v}" for k, v in changes.items())
    verdict = "todo"
    if "status" in changes and changes["status"] != old["status"]:
        verdict, conclusion = _status_conclusion(old["status"], changes["status"])
        note = f"{conclusion}；{note}"
    src = _rows(db, "SELECT * FROM todo_sources WHERE todo_id=:t ORDER BY id", t=todo_id)
    with db.write() as s:
        sets = ", ".join(f"{k}=:{k}" for k in changes)
        _exec(s, f"UPDATE todo_items SET {sets}, updated_at=:now WHERE id=:id", **changes, now=now, id=todo_id)
        _exec(s, "INSERT INTO feedback(action,todo_id,msg_id,note,created_at) VALUES('edit',:t,:m,:no,:n)",
              t=todo_id, m=src[0]["msg_id"] if src else None, no=note, n=now)
        _sample(s, source="edit", group_id=src[0]["group_id"] if src else None,
                sender=src[0]["sender_name"] if src else "", text=_clip("\n".join(x["quote"] for x in src)),
                verdict=verdict, note=note, now=now)
    return _todo_out(db, _get_todo(db, todo_id))


# ------------------------------------------------------------------ 手动添加待办
_MANUAL_TITLE_MAX = 120  # 与详情页标题 Input maxLength=120 一致，避免"能改不能建"
_MANUAL_DETAIL_MAX = 2000


@router.post("/todos", status_code=201)
async def create_todo(request: Request) -> dict:
    """手动添加。origin 写死 manual、status 写死 open，前端传了也忽略。
    不写判别样本（_sample）：手动条目没有群消息原文，写进去只会污染样本。"""
    _, db = _ctx_db(request)
    body = await _json(request)
    title = str(body.get("title") or "").strip()
    if not title:
        raise _bad("添加待办失败：标题不能为空")
    if len(title) > _MANUAL_TITLE_MAX:
        raise _bad(f"添加待办失败：标题最多 {_MANUAL_TITLE_MAX} 字")

    detail = str(body.get("detail") or "").strip()
    if len(detail) > _MANUAL_DETAIL_MAX:
        raise _bad(f"添加待办失败：备注最多 {_MANUAL_DETAIL_MAX} 字")

    due_at = body.get("due_at") or None
    if due_at is not None:
        due_at = str(due_at).strip()
        try:
            datetime.fromisoformat(due_at.replace("Z", "+00:00"))  # 只校验，原样存，与 patch_todo 口径一致
        except ValueError:
            raise _bad("添加待办失败：截止时间格式不对") from None

    now = now_iso(db)
    with db.write() as s:
        # 手动条目没有群消息原文：block 占位、infos 为空；confidence 与人工改判同口径（1.0）
        todo_id = _create_todo(s, title=title, detail=detail, due_at=due_at, confidence=1.0,
                               block={"id": None}, infos=[], now=now, origin="manual")
    return _todo_out(db, _get_todo(db, todo_id))


@router.get("/review")
def list_review(request: Request) -> list:
    _, db = _ctx_db(request)
    out = []
    for r in _rows(db, "SELECT r.*, b.msg_ids, b.text AS block_text FROM review_items r "
                       "LEFT JOIN message_blocks b ON b.id=r.block_id WHERE r.status='pending' ORDER BY r.id DESC"):
        infos = msg_infos(db, json.loads(r["msg_ids"])) if r.get("msg_ids") else []
        first = infos[0] if infos else {}
        text = r["block_text"] if r.get("block_text") is not None else (r.get("evidence") or "")
        out.append({k: r[k] for k in ("id", "block_id", "msg_id", "confidence", "question", "evidence",
                                      "status", "created_at")}
                   | {"source": r.get("source") or "message",
                      "ask_reason": r.get("ask_reason"),
                      "time_text": r.get("time_text"),
                      "chat_message_id": r.get("chat_message_id"),
                      "preview": {"group_name": first.get("group_name", ""),
                                  "sender_name": first.get("sender_name", ""), "text": text},
                      "suggestion": _parse_suggestion(r.get("suggestion"))})
    return out


@router.post("/review/{review_id}/resolve")
async def resolve_review(review_id: int, request: Request) -> dict:
    _, db = _ctx_db(request)
    body = await _json(request)
    action = body.get("action")
    if action not in ("confirm", "ignore"):
        raise _bad("处理待确认失败：action 只能是 confirm 或 ignore")
    rows = _rows(db, "SELECT r.*, b.msg_ids, b.text AS block_text, b.group_id AS bgid FROM review_items r "
                     "LEFT JOIN message_blocks b ON b.id=r.block_id WHERE r.id=:i", i=review_id)
    if not rows:
        raise _bad("待确认事项不存在", 404)
    r = rows[0]
    if r["status"] != "pending":
        raise _bad("这条待确认已经处理过了，不能重复处理")
    if r.get("source") == "chat" and action == "confirm":
        from app import event_route

        due = event_route.parse_due(body.get("due_at"), datetime.fromisoformat(now_iso(db)))
        if due is None or due.date() < datetime.fromisoformat(now_iso(db)).date():
            raise _bad("确认失败：先选一个具体日期")
    raw_ids = r.get("msg_ids")
    infos = msg_infos(db, json.loads(raw_ids)) if raw_ids else []  # 对话条目没有消息块
    src_text = str(r.get("block_text") or r.get("evidence") or "")
    sender = infos[0]["sender_name"] if infos else ""
    now = now_iso(db)
    todo_id = None
    title = ""
    with db.write() as s:
        cur = s.execute(sql("UPDATE review_items SET status=:st, resolved_at=:n WHERE id=:i AND status='pending'"),
                        {"st": "confirmed" if action == "confirm" else "ignored", "n": now, "i": review_id})
        if cur.rowcount != 1:
            raise _bad("这条待确认已经处理过了，不能重复处理")
        if action == "confirm":
            title = str(body.get("title") or "").strip() or src_text.replace("\n", " ")[:30]
            due_iso = due.isoformat() if r.get("source") == "chat" and due is not None else body.get("due_at")
            todo_id = _create_todo(s, title=title, detail=str(body.get("detail") or ""),
                                   due_at=due_iso, confidence=r["confidence"],
                                   block={"id": r["block_id"]}, infos=infos, now=now, origin="review")
        _exec(s, "INSERT INTO feedback(action,review_id,todo_id,msg_id,note,created_at) "
                 "VALUES(:a,:r,:t,:m,'',:n)", a=action, r=review_id, t=todo_id, m=r["msg_id"], n=now)
        _sample(s, source=action, group_id=r["bgid"], sender=sender, text=src_text,
                verdict="todo" if action == "confirm" else "ignored", note="", now=now)
    if todo_id is not None:
        hub.publish({"type": "todo.created", "todo_id": todo_id, "msg_id": r["msg_id"], "title": title})
    return {"ok": True, "review_id": review_id, "status": "confirmed" if action == "confirm" else "ignored",
            "todo_id": todo_id}


CHAT_COLS = "id, role, kind, text, ref_review_id, ref_msg_id, created_at"


@router.get("/chat/messages")
def chat_list(request: Request, before_id: int | None = None, limit: int = 50) -> list:
    _, db = _ctx_db(request)
    return _rows(db, f"SELECT {CHAT_COLS} FROM chat_messages WHERE (:b IS NULL OR id < :b) "
                     "ORDER BY id DESC LIMIT :l", b=before_id, l=max(1, min(limit, 200)))


def _rule_result_line(rule: dict, outcome: str) -> str:
    rid = int(rule["id"])
    waiting = rule.get("status") == "suggested"
    if outcome == "created":
        return "规则已生效：这类消息以后不打扰你" if not waiting else "规则已提交，等你确认"
    if outcome == "updated":
        return f"已更新规则 #{rid}" + ("，等你确认" if waiting else "")
    return f"规则 #{rid} 没有变化" + ("，仍在等你确认" if waiting else "，本来就是这样")


def _chat_row(db: Any, i: int) -> dict:
    return _rows(db, f"SELECT {CHAT_COLS} FROM chat_messages WHERE id=:i", i=i)[0]


def _post_assistant_text(db: Any, text: str) -> int:
    with db.write() as s:
        aid = _exec(s, "INSERT INTO chat_messages(role,kind,text,created_at) VALUES('assistant','text',:t,:n)",
                    t=text, n=now_iso(db))
    hub.publish({"type": "chat.message", "id": aid, "role": "assistant", "kind": "text"})
    return aid


def _log_chat_event(db: Any, *, asked: int, grain: str, status: str, event_kind: str,
                    history_used: int, extract_tried: int = 0, extract_result: str = "",
                    intent_status: str = "", intent_kinds: str = "", intent_attempts: int = 0) -> None:
    """/045C/046：一行观测记录。失败只记日志，不影响对话。"""
    with db.write() as s:
        _exec(s, "INSERT INTO chat_event_log(created_at,asked,grain,marker_status,event_kind,history_used,"
                 "extract_tried,extract_result,intent_status,intent_kinds,intent_attempts) "
                 "VALUES(:n,:a,:g,:st,:k,:h,:et,:er,:ist,:iks,:iat)",
              n=now_iso(db), a=asked, g=grain, st=status, k=event_kind, h=history_used,
              et=extract_tried, er=extract_result, ist=intent_status, iks=str(intent_kinds)[:200],
              iat=int(intent_attempts))


def _turn_reply(turn: str, lines: list[str], chat_text: str, answer: str) -> str:
    """按轮次三选一：纯聊天只显示模型正文；写入只显示回执行；混合是回执行 + 空行 + 回答。"""
    if turn == "chat":
        return chat_text or CHAT_FALLBACK_REPLY
    body = "\n".join(lines)
    return f"{body}\n\n{answer}" if (turn == "mixed" and answer) else body


async def _run_extract(db: Any, ctx: Any, text: str) -> dict | str | None:
    """全对话唯一的抽取调用点。同步/异步都兼容；任何异常都算技术失败。"""
    from app import chat_llm

    try:
        got = chat_llm.extract_event_spec(db, ctx, text)
        if inspect.isawaitable(got):
            got = await got
    except Exception as exc:
        log.warning("事件抽取异常：%s", type(exc).__name__)
        return None
    if isinstance(got, dict):
        return got
    return "none" if got == "none" else None


@router.post("/chat/messages")
async def chat_send(request: Request) -> dict:
    """用户发话：落库 user → 记忆 → 意图 ∥ 说话 → 执行 → 按轮次三选一 → 落库 assistant → 确认卡。

    状态陈述只出自回执（执行器回读的结果）；模型文字不承担状态。不重判已生成的待办/待确认（产品约定）。
    """
    from app import agent_memory, chat_intent, chat_llm, event_route, intent_exec, receipt_view

    ctx, db = _ctx_db(request)
    body = await _json(request)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _bad("发送失败：消息不能为空")
    if len(text) > 2000:
        raise _bad("发送失败：消息不能超过 2000 字")
    with db.write() as s:
        uid = _exec(s, "INSERT INTO chat_messages(role,kind,text,created_at) VALUES('user','text',:t,:n)",
                    t=text, n=now_iso(db))
    hub.publish({"type": "chat.message", "id": uid, "role": "user", "kind": "text"})
    mem_id: int | None = None
    try:
        mem_id = agent_memory.capture_from_chat(db, text, uid)
    except Exception as exc:
        log.warning("记忆提取异常：%s", type(exc).__name__)

    # 1) 意图 ∥ 说话：正文本步骤不依赖意图结果，延迟≈较慢的一次；needs_answer 只决定要不要第二次调用
    intent_res, talk = await asyncio.gather(
        chat_intent.classify(db, ctx, text),
        chat_llm.talk(db, ctx, text, before_id=uid),
        return_exceptions=True,
    )
    if isinstance(intent_res, BaseException):
        log.warning("意图调用异常：%s", type(intent_res).__name__)
        intent_res = chat_intent.unclear_result(text, f"异常:{type(intent_res).__name__}")
    if isinstance(talk, BaseException):
        log.warning("说话调用异常：%s", type(talk).__name__)
        talk = chat_llm.Talk()

    # 2) 按意图执行（各执行器内部已逐个兜底，这里只防整体崩）
    try:
        ex = await intent_exec.run(db, ctx, text, uid, intent_res)
    except Exception as exc:
        log.warning("意图执行异常：%s", type(exc).__name__)
        ex = intent_exec.ExecResult(receipts=[intent_exec.Receipt("crash", "", "failed", why="系统内部出错")])

    # 3) 观测：失败不影响对话
    try:
        _log_chat_event(db, asked=1 if event_route.is_asked(text, text) else 0,
                        grain=event_route.program_grain(text), status="",  # 旧 EVENT 标记状态已退役
                        event_kind=ex.event_kind or "",
                        history_used=0 if chat_llm.EXPERIMENT_NO_HISTORY else 1,
                        extract_tried=ex.extract_tried, extract_result=ex.extract_result,
                        intent_status=intent_res.status, intent_kinds=intent_res.kinds,
                        intent_attempts=intent_res.attempts)
    except Exception as exc:
        log.warning("事件观测记录失败：%s", type(exc).__name__)

    # 4) 回执 → 三选一：纯聊天（无回执行）/ 写入（有回执、没有要答的）/ 混合（有回执、还要回答）
    receipts = receipt_view.collect(ex, pure_none=intent_res.pure_none)
    lines = receipt_view.render(receipts)
    turn = "chat" if not lines else ("mixed" if talk.needs_answer else "write")
    answer = ""
    if turn == "mixed":
        try:
            answer = await chat_llm.answer_with_facts(db, ctx, text, before_id=uid,
                                                      facts=receipt_view.fact_note(receipts))
        except Exception as exc:
            log.warning("混合轮次第二次调用异常：%s（只显示回执）", type(exc).__name__)
    final = _turn_reply(turn, lines, talk.text, answer)

    # 5) 分歧兜底：两次独立判断对账，有分歧就出卡，不猜、不静默
    diverge = intent_exec.divergence_card(talk.user_wants_change, intent_res.pure_none,
                                          bool(receipts or lines), ex.pending, text)
    if diverge is not None:
        ex.pending.append(diverge)
    log.info("轮次=%s 回执=%s 分歧卡=%s", turn, [(r.kind, r.state) for r in receipts],
             diverge is not None)

    with db.write() as s:
        aid = _exec(s, "INSERT INTO chat_messages(role,kind,text,created_at) VALUES('assistant','text',:t,:n)",
                    t=final, n=now_iso(db))
    if mem_id is not None:
        try:
            agent_memory.mark_turn(db, aid, mem_id)
        except Exception as exc:
            log.warning("记忆轮次标记异常：%s", type(exc).__name__)
    hub.publish({"type": "chat.message", "id": aid, "role": "assistant", "kind": "text"})
    cards = intent_exec.post_cards(db, ex.pending, user_id=uid) if ex.pending else []
    return {"user": _chat_row(db, uid), "assistant": _chat_row(db, aid), "cards": cards}


@router.get("/chat/confirms")
def chat_confirms(request: Request, status: str = "pending") -> dict:
    """未处理的确认卡（5）：助手页刷新后恢复按钮。只支持 status=pending，按 id 升序。

    choices 由 intent_exec.choices_for(kind, topic) 生成；message 为对应 kind='confirm' 的助手消息行
    （已被保留期清理则为 null）。整行复用 intent_exec.card_payload，所以和 POST 返回的 cards[]、
    以及 /api/chat/messages 里的消息行形状一致 —— 前端两条路径（新消息 / 刷新恢复）走同一套渲染。
    """
    from app import intent_exec

    _, db = _ctx_db(request)
    if status != "pending":
        raise _bad("只支持 status=pending")
    rows = _rows(db, "SELECT * FROM chat_confirms WHERE status='pending' ORDER BY id ASC")
    return {"items": [intent_exec.card_payload(db, r) for r in rows]}


@router.post("/chat/confirms/{confirm_id}")
async def chat_confirm(confirm_id: int, request: Request) -> dict:
    """确认卡点击。UPDATE…WHERE status='pending' + 行数检查保证幂等：重复点击只执行一次。"""
    from app import intent_exec, receipt_view

    ctx, db = _ctx_db(request)
    body = await _json(request)
    choice = body.get("choice")
    if choice not in intent_exec.CHOICES:
        raise _bad("操作失败：choice 只能是 accept / review / dismiss")
    rows = _rows(db, "SELECT * FROM chat_confirms WHERE id=:i", i=confirm_id)
    if not rows:
        raise HTTPException(status_code=404, detail="确认卡不存在")
    card = rows[0]
    if card["kind"] not in intent_exec.CARD_KINDS:
        raise _bad("操作失败：未知的确认卡类型")
    if choice not in {v for v, _l in intent_exec.choices_for(card["kind"], card.get("topic") or "")}:
        raise _bad("操作失败：这张卡没有这个选项")
    with db.write() as s:
        n = s.execute(sql("UPDATE chat_confirms SET status=:st, resolved_at=:n WHERE id=:i AND status='pending'"),
                      {"st": "dismissed" if choice == "dismiss" else "accepted",
                       "n": now_iso(db), "i": confirm_id}).rowcount
    fresh = lambda: _rows(db, "SELECT * FROM chat_confirms WHERE id=:i", i=confirm_id)[0]  # noqa: E731
    if not n:  # 已经处理过：不重复执行
        return {"confirm": intent_exec.card_payload(db, fresh()), "assistant": None, "already": True}
    try:
        ex = await intent_exec.resolve(db, ctx, card, choice)
    except Exception as exc:  # 执行整体崩了：放回 pending，允许再点
        log.warning("确认卡执行异常：%s", type(exc).__name__)
        with db.write() as s:
            s.execute(sql("UPDATE chat_confirms SET status='pending', resolved_at=NULL WHERE id=:i"),
                      {"i": confirm_id})
        raise _bad("操作失败：系统内部出错，可以再点一次") from None
    aid = _post_assistant_text(db, receipt_view.card_reply(ex, intent_exec.DISMISS_LINE))
    return {"confirm": intent_exec.card_payload(db, fresh()), "assistant": _chat_row(db, aid), "already": False}

@router.get("/memory/samples")
def samples_list(request: Request, limit: int = 100) -> list:
    _, db = _ctx_db(request)
    return _rows(db, "SELECT id, source, group_id, sender_name, text, verdict, note, created_at "
                     "FROM memory_samples ORDER BY id DESC LIMIT :l", l=max(1, min(limit, 500)))


@router.delete("/memory/samples/{sample_id}")
def samples_delete(sample_id: int, request: Request) -> dict:
    _, db = _ctx_db(request)
    with db.write() as s:
        n = s.execute(sql("DELETE FROM memory_samples WHERE id=:i"), {"i": sample_id}).rowcount
    if not n:
        raise _bad("记忆样本不存在", 404)
    return {"ok": True, "id": sample_id}




@router.get("/focus-topics")
def focus_topics_list(request: Request) -> dict:
    from app import focus_topics

    _, db = _ctx_db(request)
    return {"items": focus_topics.list_topics(db)}


@router.post("/focus-topics")
async def focus_topics_add(request: Request) -> dict:
    from app import focus_topics

    _, db = _ctx_db(request)
    body = await _json(request)
    row, outcome = focus_topics.add_manual(db, body.get("topic"))
    if row is None:
        raise _bad("关注话题不能为空，且不超过 20 字")
    if outcome == "created":
        hub.publish({"type": "focus.created", "focus_id": row["id"]})
    return {"item": row, "outcome": outcome}


@router.delete("/focus-topics/{fid}")
def focus_topics_delete(fid: int, request: Request) -> dict:
    from app import focus_topics

    _, db = _ctx_db(request)
    gone = focus_topics.delete(db, fid)
    if gone is None:
        raise _bad("删除失败：这个关注话题已经不在了")
    hub.publish({"type": "focus.deleted", "focus_id": fid})
    return {"deleted": gone}


@router.get("/memory/profile")
def profile_get(request: Request) -> dict:
    _, db = _ctx_db(request)
    out = _profile(db)
    out.pop("pending", None)
    out["can_undo"] = profile_can_undo(db)
    return out


@router.put("/memory/profile")
async def profile_put(request: Request) -> dict:
    ctx, db = _ctx_db(request)
    body = await _json(request)
    if not isinstance(body, dict):
        raise _bad("保存画像失败：请求体必须是 JSON 对象")
    # 只认 about；grade/class_name/major/roles 已退役，传了也静默忽略
    v = body.get(PROFILE_ABOUT_KEY)
    if v is not None:
        if not isinstance(v, str):
            raise _bad("保存画像失败：about 必须是字符串")
        about = _normalize_about(v)
        n = len(about)
        if n > PROFILE_ABOUT_MAX_CHARS:
            raise _bad(f"保存画像失败：自我介绍最多 {PROFILE_ABOUT_MAX_CHARS} 字，当前 {n} 字，请删减后再保存")
        written, _why = _profile_write(db, about, evidence=[], source="manual")
        if written is not None:
            hub.publish({"type": "profile.updated", "history_id": written["history_id"]})
    return _profile(db)


# ------------------------------------------------------------------ 挂载


@router.post("/memory/profile/undo")
def profile_undo_api(request: Request) -> dict:
    from fastapi import HTTPException

    _, db = _ctx_db(request)
    res = profile_undo(db)
    if res is None:
        raise HTTPException(status_code=409, detail="撤销失败：没有可以撤销的画像改动")
    hub.publish({"type": "profile.updated", "undone_id": res["undone_id"]})
    return res

def _hit_out(h: dict) -> dict:
    return {"id": h["id"], "rule_id": h["rule_id"], "msg_id": h["msg_id"], "block_id": h["block_id"],
            "verdict_before": h["verdict_before"], "restored": bool(h["restored"]),
            "created_at": h["created_at"], "text": str(h.get("text") or "")[:200]}


def _r_from_snapshot(snap: dict, vb: str) -> dict:
    routed = snap.get("routed")
    routed = ([tuple(p) for p in routed if isinstance(p, (list, tuple)) and len(p) == 2]
              if isinstance(routed, list) else None) or None  # 空列表按单事项落库，保证有东西回来
    return {"stage": snap.get("stage") or "llm", "verdict": vb, "reason": snap.get("reason") or "",
            "confidence": float(snap.get("confidence") or 0), "evidence": snap.get("evidence") or "",
            "provider": snap.get("provider") or "", "model": snap.get("model") or "",
            "raw": snap.get("raw") or "", "item": snap.get("item"), "route": snap.get("route"),
            "routed": routed}


def restore_rule_hit(db: Any, hit_id: int) -> dict | None:
    """把被「不看」吞掉的消息按原判重新落库。不存在 → None；已恢复 → 幂等返回。"""
    rows = _rows(db, "SELECT * FROM rule_hits WHERE id=:i", i=hit_id)
    if not rows:
        return None
    hit = rows[0]
    if hit["restored"]:
        return {"hit": _hit_out(hit), "restored_now": False, "events": []}
    try:
        snap = json.loads(hit["snapshot"] or "{}")
    except ValueError:
        snap = {}
    blocks = _rows(db, "SELECT * FROM message_blocks WHERE id=:b", b=hit["block_id"])
    block = blocks[0] if blocks else None
    msg_ids = json.loads(block["msg_ids"]) if block else []
    infos = msg_infos(db, msg_ids) if block else []  # 写事务外先读好
    vb = hit["verdict_before"]
    now = now_iso(db)
    events: list[dict] = []
    restored_now = False
    with db.write() as s:
        # 抢占式标记：并发两次恢复只有一次 rowcount=1，另一次什么都不建
        if s.execute(sql("UPDATE rule_hits SET restored=1 WHERE id=:i AND restored=0"),
                     {"i": hit_id}).rowcount != 1:
            pass
        else:
            restored_now = True
            cur = s.execute(sql("SELECT verdict FROM decisions WHERE block_id=:b ORDER BY id DESC LIMIT 1"),
                            {"b": hit["block_id"]}).scalar()
            # 已经被人工改判过（不再是 ignored）就只记已恢复，不再建，防重复
            if block is not None and cur == "ignored" and vb in ("todo", "review"):
                r2 = _r_from_snapshot(snap, vb)
                _land(s, block=block, infos=infos, first_id=msg_ids[0] if msg_ids else None,
                      r=r2, now=now, events=events)
                why = f"你说这条不该被「不看」#{hit['rule_id']} 忽略，已按原判恢复"
                route = _route_info(vb, "restore", why, rule_id=int(hit["rule_id"]), model=snap.get("route"))
                s.execute(sql("UPDATE decisions SET verdict=:v, route=:rt WHERE block_id=:b"),
                          {"v": vb, "rt": json.dumps(route, ensure_ascii=False), "b": hit["block_id"]})
    for ev in events:
        hub.publish(ev)
    log.info("命中恢复：hit=%s 本次恢复=%s 原判=%s 事件数=%d", hit_id, restored_now, vb, len(events))
    hit["restored"] = 1
    return {"hit": _hit_out(hit), "restored_now": restored_now, "events": events}


@router.get("/agent/rules/{rule_id}/hits")
def rule_hits_list(request: Request, rule_id: int, limit: int = 50) -> list:
    _, db = _ctx_db(request)
    rows = _rows(db, "SELECT h.*, b.text AS text FROM rule_hits h LEFT JOIN message_blocks b "
                     "ON b.id = h.block_id WHERE h.rule_id=:r ORDER BY h.id DESC LIMIT :n",
                 r=rule_id, n=max(1, min(int(limit), 200)))
    if not rows and not _rows(db, "SELECT id FROM user_rules WHERE id=:r", r=rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return [_hit_out(h) for h in rows]


@router.post("/agent/rules/hits/{hit_id}/restore")
async def rule_hit_restore(request: Request, hit_id: int) -> dict:  # async：hub.publish 留在事件循环里
    _, db = _ctx_db(request)
    out = restore_rule_hit(db, hit_id)
    if out is None:
        raise HTTPException(status_code=404, detail="命中记录不存在")
    return out


async def ws_events(websocket: WebSocket) -> None:
    """实时事件通道：连上先发 hello，之后推送 message/todo/review/chat 事件。

    鉴权用 query 参数 token（浏览器 WS 带不了自定义头）；令牌错关闭码 4401，
    前端据此提示"令牌无效"而不是无限重连。
    """
    ctx = websocket.app.state.ctx
    expected = getattr(getattr(ctx, "settings", None), "access_token", None) or _env("QN_ACCESS_TOKEN", "")
    if not token_matches(websocket.query_params.get("token", ""), expected):
        await websocket.close(code=4401)
        return
    hub.bind_loop()
    await hub.connect(websocket, hello={"type": "hello", "server": "qnotify",
                                        "events": ["message.created", "todo.created",
                                                   "review.created", "chat.message"]})
    try:
        while True:  # 只用于感知断开；客户端不需要发消息
            await websocket.receive_text()
    except Exception:
        return
    finally:
        hub.disconnect(websocket)


def install(app: FastAPI) -> None:
    """挂到 create_app 返回之前：M2 路由排最前，第一个请求时建表。"""
    n = len(app.router.routes)
    # 整条路由器挂作用域兜底，现有与以后新增的 m2 路由都自动继承，
    # 于是这些路由里 hub.publish 发出的业务事件会自动带 account_id。
    app.include_router(router, dependencies=[Depends(deps.bind_scope_quietly)])
    app.add_api_websocket_route("/ws/events", ws_events)
    new = app.router.routes[n:]
    del app.router.routes[n:]
    app.router.routes[0:0] = new

    @app.middleware("http")
    async def _m2_schema(request: Request, call_next):  # type: ignore[no-untyped-def]
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is not None:
            try:
                ensure_schema(deps.get_db(ctx))
            except Exception:
                pass
        return await call_next(request)

    from app.adapters import api as adapter_api

    adapter_api.install(app)  # 适配器路由排到最前（含 /api/selfcheck 接管）


async def auto_loop(db: Any, ctx: Any) -> None:
    """后台自动处理：每个账号在自己的库里判定；顺带驱动每日保留期清理。

    - 登录号只在**变化时** touch 一次（线程池），不再每轮写 last_seen_at
    - 每个账号判定期间绑定作用域，flush 里发出的 hub 事件自动带 account_id
    - 清理是独立 task（CleanupScheduler.tick 立即返回），不会拖慢判定
    """
    from . import accounts

    interval = 5.0
    try:
        s = getattr(ctx, "settings", None)
        interval = float(getattr(s, "auto_flush_seconds", 5.0) or 5.0)
    except Exception:
        pass
    interval = max(0.2, interval)
    last_live: int | None = None
    cleanup = lifecycle.CleanupScheduler(db)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                cleanup.tick()  # 只判断是否到点，到点则 create_task，立即返回

                live = accounts.live_self_id_of(getattr(ctx, "napcat", None))
                if live is not None and live != last_live:
                    try:
                        await asyncio.to_thread(accounts.touch, db, live)
                        last_live = live
                    except Exception as exc:  # 建号失败不该影响判定
                        log.warning("按登录态建账号失败：%s", type(exc).__name__)

                targets = await asyncio.to_thread(accounts.list_ids, db)
                for acc_id in targets:
                    try:
                        target = await asyncio.to_thread(accounts.db_for, db, acc_id)
                    except accounts.AccountNotFound:  # 本轮中途被删
                        continue
                    if not m2_settings(target)["pipeline_enabled"]:
                        continue
                    token = accounts.bind_scope_account(acc_id)
                    try:
                        await flush(target, ctx)
                    finally:
                        accounts.reset_scope_account(token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 自动处理失败不能拖垮服务
                log.warning("自动处理失败：%s", type(exc).__name__)
    finally:
        await cleanup.aclose()
