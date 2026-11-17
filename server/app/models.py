"""表结构与领域数据类型。

建表 DDL 只增不改；字段、约束、索引与迁移逻辑保持一致。
这里不用 ORM 自动建表，因为 SQLAlchemy 方言可能改写类型或约束，导致和建表语句不一致。
查询统一走 SQLAlchemy Core 的 text()，配合同步 Session 执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS groups (
        group_id     INTEGER PRIMARY KEY,
        group_name   TEXT    NOT NULL DEFAULT '',
        member_count INTEGER NOT NULL DEFAULT 0,
        updated_at   TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS members (
        group_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        nickname   TEXT    NOT NULL DEFAULT '',
        card       TEXT    NOT NULL DEFAULT '',
        role       TEXT    NOT NULL DEFAULT 'member',
        updated_at TEXT    NOT NULL,
        PRIMARY KEY (group_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS friends (
        user_id    INTEGER PRIMARY KEY,
        nickname   TEXT    NOT NULL DEFAULT '',
        remark     TEXT    NOT NULL DEFAULT '',
        updated_at TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whitelist_groups (
        group_id   INTEGER PRIMARY KEY,
        mode       TEXT    NOT NULL CHECK (mode IN ('all', 'members')),
        updated_at TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whitelist_members (
        group_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        PRIMARY KEY (group_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whitelist_users (
        user_id INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id       INTEGER NOT NULL,
        group_id     INTEGER,
        user_id      INTEGER NOT NULL,
        message_type TEXT    NOT NULL,
        sender_name  TEXT    NOT NULL DEFAULT '',
        time         INTEGER NOT NULL,
        text         TEXT    NOT NULL DEFAULT '',
        raw_json     TEXT    NOT NULL,
        via          TEXT    NOT NULL,
        ingested_at  TEXT    NOT NULL,
        UNIQUE (message_type, msg_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_group_time ON messages (group_id, time DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_time ON messages (time DESC, id DESC)",
    """
    CREATE TABLE IF NOT EXISTS counters (
        name  TEXT    PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

# ---- 维护记录（通用表，只建在主库，不进 SCHEMA_STATEMENTS，免得每个账号库都长一张）----
MAINTENANCE_LOG_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS maintenance_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT NOT NULL,
        detail     TEXT NOT NULL,
        ok         INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

# 只在主库、不属于任何账号的表。accounts.SHARED_TABLES 必须包含它们，否则删主库归属账号会把它们一起清掉。
GLOBAL_ONLY_TABLES: frozenset[str] = frozenset({"maintenance_log"})

MAINT_BACKUP = "backup"
MAINT_RESTORE = "restore"
MAINT_CLEANUP = "cleanup"
MAINT_DELETE_ACCOUNT = "delete_account"
MAINT_KINDS = frozenset({MAINT_BACKUP, MAINT_RESTORE, MAINT_CLEANUP, MAINT_DELETE_ACCOUNT})

# ---- 备份设置（存 settings 表，键带 "backup." 前缀；不并入 SETTING_KEYS，/api/settings 行为不变）----
BACKUP_SETTING_DEFAULTS: dict[str, str] = {
    "backup.enabled": "true",
    "backup.interval_hours": "24",
    "backup.keep": "10",
    "backup.keep_preop": "3",
    "backup.dir": "",
}
BACKUP_SETTING_KEYS: tuple[str, ...] = tuple(BACKUP_SETTING_DEFAULTS)

# ---- 保留期（主库 settings 表，键带 "retention." 前缀；不并入 SETTING_KEYS）----
RETENTION_SETTING_DEFAULTS: dict[str, str] = {
    "retention.overdue_days": "7",
    "retention.content_days": "30",
}
RETENTION_SETTING_KEYS: tuple[str, ...] = tuple(RETENTION_SETTING_DEFAULTS)
# 取值范围（含端点）。存量越界值按默认处理，写入时直接拒绝。
RETENTION_INT_RANGES: dict[str, tuple[int, int]] = {
    "retention.overdue_days": (1, 30),
    "retention.content_days": (7, 365),
}
RETENTION_ACKED_AT_KEY = "retention.expired_acked_at"  # "N 件事已收起"提示的已读时间
RETENTION_LAST_RUN_KEY = "retention.last_run_at"       # 上一轮清理完成时间（重启后不重跑）
DONE_TODO_KEEP_DAYS = 90                               # 已完成/不用做的待办保留天数（固定，不设开关）

# ---- 固定名字 ----
COUNTER_MESSAGES_DROPPED = "messages_dropped"

MESSAGE_TYPE_GROUP = "group"
MESSAGE_TYPE_PRIVATE = "private"

VIA_LIVE = "live"
VIA_BACKFILL = "backfill"

MODE_ALL = "all"
MODE_MEMBERS = "members"
WHITELIST_MODES = frozenset({MODE_ALL, MODE_MEMBERS})

MEMBER_ROLES = frozenset({"owner", "admin", "member"})

SETTING_KEYS: tuple[str, ...] = (
    "llm_base_url",
    "llm_model",
    "llm_api_key",
    "backfill_enabled",
    "backfill_count",
)

SECRET_MASK = "***"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """已解析的入库候选消息（事件解析的产物，闸门和入库都只认它）。"""

    msg_id: int
    message_type: str
    group_id: int | None
    user_id: int
    sender_name: str
    time: int
    text: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WhitelistSnapshot:
    """白名单的内存快照。闸门只读这个对象，不碰数据库。"""

    groups: dict[int, str] = field(default_factory=dict)
    members: dict[int, frozenset[int]] = field(default_factory=dict)
    users: frozenset[int] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return not self.groups and not self.users


# ---- 画像提议（每个账号库都要有，跟 profile 表放一起）----
# 「不看」命中记录。snapshot 是在建表语句之外加的一列：
# 恢复时要用原判的事项（标题、截止时间、逐事项去向）重新落库，光有 verdict_before 不够。
RULE_HITS_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS rule_hits (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id        INTEGER NOT NULL,
        msg_id         INTEGER,
        block_id       INTEGER,
        verdict_before TEXT    NOT NULL DEFAULT '',
        restored       INTEGER NOT NULL DEFAULT 0,
        snapshot       TEXT    NOT NULL DEFAULT '{}',
        created_at     TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rule_hits_rule ON rule_hits (rule_id, id DESC)",
)

# 一次性迁移的标记。库里已有同名表时 IF NOT EXISTS 不会动它；写入只用 key/value 两列。
APP_KV_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS app_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')",
)

# 画像提议已退役，表一起删掉
RETIRED_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_profile_proposals_status",
    "DROP TABLE IF EXISTS profile_proposals",
)


PROFILE_HISTORY_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS profile_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        before_about TEXT NOT NULL DEFAULT '',
        after_about  TEXT NOT NULL DEFAULT '',
        evidence     TEXT NOT NULL DEFAULT '[]',
        source       TEXT NOT NULL DEFAULT 'chat',
        created_at   TEXT NOT NULL,
        undone_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_profile_history_undone ON profile_history (undone_at, id DESC)",
)


# ---- 对话事件观测（只记录，不参与业务）----
CHAT_EVENT_LOG_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS chat_event_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT    NOT NULL,
        asked         INTEGER NOT NULL DEFAULT 0,   -- 触发词命中
        grain         TEXT    NOT NULL DEFAULT 'none',
        marker_status TEXT    NOT NULL DEFAULT 'missing',
        event_kind    TEXT    NOT NULL DEFAULT '',  -- todo / review / dup / drop / ''
        history_used  INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_event_log_at ON chat_event_log (created_at DESC)",
)


# ---- 意图层确认卡（待用户点［加］［移除］［记］［恢复］的那些意图）----
CHAT_CONFIRMS_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS chat_confirms(
  id INTEGER PRIMARY KEY,
  chat_message_id INTEGER NOT NULL,
  user_message_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  topic TEXT,
  target_id INTEGER,
  evidence TEXT,
  user_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  resolved_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_chat_confirms_message ON chat_confirms(chat_message_id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_confirms_status ON chat_confirms(status, id)",
)


# ---- 关注清单 ----
FOCUS_TOPIC_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS focus_topics ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " topic TEXT NOT NULL COLLATE NOCASE,"
    " note TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " hits INTEGER NOT NULL DEFAULT 0)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_focus_topics_topic ON focus_topics(topic)",
)
