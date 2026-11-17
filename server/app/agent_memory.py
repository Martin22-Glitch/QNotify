"""要点记忆（会记、会省、会瘦身）。

全部纯规则，零模型调用：
- 提取：只对"明显的持久偏好"句子用正则抽出【条件：动作】，存成一句 ≤80 字的要点；
- 相似：NFKC 归一 + 去标点比对；字二元组 Dice ≥ 0.8，且数字、否定词必须一致才算相似；
- 注入：按 类型 + 与当前问题/上下文的相关度 + 强化次数 + 新近度 打分，最多 8 条 / 600 字；
- 整理：先合并 → 再归档 → 最后才删；rule 永不自动删除（被合并的 rule 留档为 merged）。

记忆可以带一条结构化执行规则（user_rules 表，memory_id 指回记忆）。
- upsert_with_rule：写记忆的同时写规则；记忆被并入已有记忆时，规则挂到幸存的那条上；
- delete：删记忆时在同一事务里删掉它（及合并进它的留档副本）带出的规则；
- slim：合并记忆时规则跟着改挂到幸存者。只有 kind=rule 的记忆能带规则，而 rule 永不自动删除，
  所以自动整理不会让任何规则悄悄消失。

日志只记类型、长度、条数，不记内容（记忆属于用户隐私）。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from sqlalchemy import text as sql

from app import m2, user_rules

log = logging.getLogger("app.agent_memory")

# ------------------------------------------------------------------ 参数（理由见 说明.md）
KINDS = ("rule", "preference", "fact")
KIND_RANK = {"rule": 3, "preference": 2, "fact": 1}
CAPTURE_KINDS = ("rule", "preference")  # 新增只允许这两类；fact 今后走画像
KIND_LABEL = {"rule": "规则", "preference": "偏好", "fact": "事实"}

CONTENT_MAX = 80            # 单条记忆上限（字）
EXTRACT_INPUT_MAX = 200     # 超过这个长度的用户发言不当"一句偏好"处理
INJECT_MAX_ITEMS = 8        # 注入条数上限
INJECT_MAX_CHARS = 600      # 注入正文总字数上限（不含标题行）
SIMILAR_DICE = 0.8          # 字二元组 Dice 阈值
RELEVANT = 0.34             # 记忆二元组有 ≥1/3 出现在问题里 → 算"相关"（计命中、可召回归档）
RELEVANCE_WEIGHT = 4.0
CONTEXT_FACTOR = 0.5        # 与待确认/最近消息的相关度打对折
RECENCY_DAYS = 14.0
TOTAL_MAX = 200             # 总量上限，超过即自动整理
NONRULE_ACTIVE_MAX = 60     # 偏好+事实的活跃上限，超出的低价值条目归档
RULE_ACTIVE_MAX = 12        # 活跃规则上限，超出的最久未用规则归档（不删）
IDLE_ARCHIVE_DAYS = 45      # 偏好/事实闲置多少天归档
ARCHIVE_TTL_DAYS = 30       # 偏好/事实归档后多少天删除

OUT_COLS = ("id", "kind", "content", "status", "source", "weight", "hits", "merged_into",
            "merged_from", "created_at", "updated_at", "last_hit_at", "archived_at")


# ------------------------------------------------------------------ 文本工具
_NUM_CN = set("零〇一二三四五六七八九十百千万两")
_NEG_RE = re.compile(r"不|别|勿|没|无需|禁止|免")


def normalize(text: Any) -> str:
    """全角转半角、小写、只留字母数字汉字。"辅导员每周三下午有例会。" 与不带句号的归一后相同。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(c for c in s if c.isalnum())


def _bigrams(norm: str) -> set[str]:
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _signature(norm: str) -> tuple:
    """数字签名：阿拉伯数字串 + 中文数字字符。周三≠周四、第1条≠第2条。"""
    return tuple(re.findall(r"\d+", norm)), tuple(c for c in norm if c in _NUM_CN)


def dice(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


def similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if _signature(a) != _signature(b):
        return False  # 数字不同，语义就不同
    if bool(_NEG_RE.search(a)) != bool(_NEG_RE.search(b)):
        return False  # "建待办" 与 "不建待办" 字面很像，意思相反
    return dice(a, b) >= SIMILAR_DICE


def _compatible(k1: str, k2: str) -> bool:
    """事实只和事实比；规则与偏好可以互相合并（偏好重复了规则 → 并入规则）。"""
    return (k1 == "fact") == (k2 == "fact")


def _clip(s: str, limit: int = CONTENT_MAX) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ------------------------------------------------------------------ 提取（纯规则）
_PUNCT = "。．.！!～~，,；;、 "
_FILLER = _PUNCT + "了哦啊呀吧啦嘛"
_QUESTION_END = re.compile(r"(?:[?？]|吗|么|呢)$")
_LEAD = re.compile(r"^(?:(?:那|好的?|嗯|对了|另外|还有|然后)[，,、 ]+)*(?:请|麻烦)?(?:你)?(?:帮我)?")
_PERSIST = re.compile(r"(?:从现在起|从今天起|从今以后|从此以后|从此|以后的话|以后|今后|往后|之后都)[，,、 ]*")
_PERSIST_ANY = re.compile(r"从现在起|从今天起|从今以后|从此|以后|今后|往后|之后都|每次|一律|统统|一概|通通|永远|总是")
_PREF = re.compile(r"我不想|我不需要|我不关心|我只关心|我只要|我喜欢|我更喜欢|我希望|别再|不要再|不用再|不必再")
_FACT_LEAD = re.compile(r"^(?:请|麻烦)?(?:你)?(?:帮我)?(?:记住|记一下|备忘)[:：，, ]*")
_COND_TAIL = re.compile(r"(?:的话|的时候|时候|时|就|都|也|[，,、\s])+$")
_COND_LEAD = re.compile(r"^(?:把|对于|关于|凡是|所有的?|只要是?|如果是?|要是|像)")

_ADV = r"(?:都|也|全都|全部|一律|统统|一概|通通|直接|就)*"
_NEG = r"(?:不要再|不用再|别再|不必再|不需要|不要|不用|不必|无需|别|勿|不)"
_TODO_VERB = r"(?:帮我|给我)?(?:建|创建|新建|生成|加|添加|列|记|放)"
_TODO_TAIL = r"(?:成|进|到|个)?待办"
_ACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_ADV + _NEG + r"(?:" + _TODO_VERB + r")?" + _TODO_TAIL), "不建待办"),
    (re.compile(_ADV + _NEG + r"(?:要)?(?:忽略|无视|屏蔽|过滤)"), "不要忽略"),
    (re.compile(_ADV + _NEG + r"(?:再)?(?:提醒|推送|告诉)(?:我)?"), "不提醒"),
    (re.compile(_ADV + _NEG + r"(?:再)?(?:问|询问)(?:我)?"), "不用问我"),
    (re.compile(_ADV + _TODO_VERB + _TODO_TAIL), "建待办"),
    (re.compile(_ADV + r"(?:忽略|无视|屏蔽|过滤|拦掉)(?:掉)?"), "忽略"),
    (re.compile(_ADV + r"(?:要|必须|务必|一定)?(?:提醒|告诉)我"), "要提醒我"),
    (re.compile(_ADV + r"(?:先)?问(?:一下)?我"), "先问我"),
]


def _find_action(s: str) -> tuple[str, int, int] | None:
    """取最靠句尾的动作（中文偏好句动作在后）；同终点取起点最早（带上"都/不要"等修饰）。"""
    best: tuple[tuple[int, int], str, int, int] | None = None
    for pat, label in _ACTIONS:
        for m in pat.finditer(s):
            if not m.group():
                continue
            key = (-m.end(), m.start())
            if best is None or key < best[0]:
                best = (key, label, m.start(), m.end())
    return (best[1], best[2], best[3]) if best else None


def extract(text: Any) -> dict[str, str] | None:
    """把一句话压成要点：'以后辅导员发的讲座都不要建待办' → rule『辅导员发的讲座：不建待办』。

    宁缺毋滥：问句、长段落、没有持久/偏好标记、没有条件的泛泛之词一律不提取。
    """
    s = " ".join(str(text or "").split())
    if not s or len(s) > EXTRACT_INPUT_MAX:
        return None
    s = s.strip(_PUNCT)
    if not s or _QUESTION_END.search(s):
        return None
    fact = _FACT_LEAD.match(s)
    body = s[fact.end():] if fact else s
    persist = bool(_PERSIST_ANY.search(body))
    pref = bool(_PREF.search(body))
    core = _PERSIST.sub("", _LEAD.sub("", body, count=1), count=1).strip(_PUNCT)
    found = _find_action(core)
    if found and (persist or pref or fact):
        label, start, end = found
        cond = _COND_LEAD.sub("", _COND_TAIL.sub("", core[:start])).strip(_PUNCT)
        if not cond:
            return None  # "以后都别提醒我"：没有条件，太泛，不当偏好存
        tail = core[end:].strip(_FILLER)
        content = f"{cond}：{label}" + (f"（{tail}）" if tail else "")
        return {"kind": "rule" if persist else "preference", "content": _clip(content)}
    if fact:
        content = (core or body).strip(_PUNCT)
        if content:
            return {"kind": "rule" if persist else "fact", "content": _clip(content)}
    return None


# ------------------------------------------------------------------ 时间工具
def _parse(ts: Any, tz: Any) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _idle_days(r: dict, now: datetime) -> float:
    dts = [d for d in (_parse(r.get(k), now.tzinfo) for k in ("created_at", "updated_at", "last_hit_at")) if d]
    if not dts:
        return 0.0
    return max(0.0, (now - max(dts)).total_seconds() / 86400)


def _age_days(ts: Any, now: datetime) -> float:
    d = _parse(ts, now.tzinfo)
    return max(0.0, (now - d).total_seconds() / 86400) if d else 0.0


def _latest_stamp(a: Any, b: Any) -> Any:
    if not a:
        return b
    if not b:
        return a
    try:
        return a if datetime.fromisoformat(str(a)) >= datetime.fromisoformat(str(b)) else b
    except (TypeError, ValueError):
        return a


# ------------------------------------------------------------------ 存取
def _load(r: dict) -> dict:
    r = dict(r)
    try:
        mf = json.loads(r.get("merged_from") or "[]")
    except (TypeError, ValueError):
        mf = []
    r["merged_from"] = [int(x) for x in mf if isinstance(x, int)] if isinstance(mf, list) else []
    r["norm"] = r.get("norm") or normalize(r.get("content"))
    r["weight"] = int(r.get("weight") or 0)
    r["hits"] = int(r.get("hits") or 0)
    return r


def _out(r: dict) -> dict:
    r = _load(r)
    return {k: r.get(k) for k in OUT_COLS}


def _priority(r: dict) -> tuple:
    """合并时谁当幸存者：规则 > 偏好 > 事实；活跃优先；强化多的优先；同分保留老 id（引用稳定）。"""
    return (KIND_RANK.get(r["kind"], 0), r["status"] == "active", r["weight"] + r["hits"], -int(r["id"]))


def _value(r: dict, now: datetime) -> float:
    return min(r["weight"] + r["hits"], 10) + 1.0 / (1.0 + _idle_days(r, now) / RECENCY_DAYS)


def count(db: Any) -> int:
    return int(m2._rows(db, "SELECT COUNT(*) n FROM agent_memory")[0]["n"])


def get(db: Any, memory_id: int) -> dict | None:
    rows = m2._rows(db, "SELECT * FROM agent_memory WHERE id=:i", i=memory_id)
    return _out(rows[0]) if rows else None


def list_all(db: Any) -> list[dict]:
    """全部记忆（含归档、被合并留档的规则），活跃在前、规则在前、新的在前。"""
    rows = m2._rows(
        db,
        "SELECT * FROM agent_memory ORDER BY "
        "CASE status WHEN 'active' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END, "
        "CASE kind WHEN 'rule' THEN 0 WHEN 'preference' THEN 1 ELSE 2 END, id DESC",
    )
    return [_out(r) for r in rows]


def _live_rows(db: Any) -> list[dict]:
    return [_load(r) for r in m2._rows(db, "SELECT * FROM agent_memory WHERE status IN ('active','archived')")]


def upsert(db: Any, kind: str, content: str, *, source: str = "manual") -> tuple[dict, bool]:
    """写入一条记忆；与已有记忆高度相似时并入（强化次数 +1），返回 (记忆, 是否并入)。"""
    content = _clip(" ".join(str(content).split()))
    norm = normalize(content)
    now = m2.now_iso(db)
    cands = [r for r in _live_rows(db) if _compatible(r["kind"], kind) and similar(r["norm"], norm)]
    if cands:
        t = max(cands, key=_priority)
        upgrade = KIND_RANK[kind] > KIND_RANK.get(t["kind"], 0)  # 偏好被用户重申成规则 → 升级
        extra = ", kind=:k, content=:c, norm=:no" if upgrade else ""
        with db.write() as s:
            s.execute(sql("UPDATE agent_memory SET weight=weight+1, updated_at=:n, status='active', "
                          f"archived_at=NULL{extra} WHERE id=:i"),
                      {"n": now, "i": t["id"], "k": kind, "c": content, "no": norm})
        return get(db, int(t["id"])) or _out(t), True
    if count(db) >= TOTAL_MAX:
        slim(db)  # 先整理再插入，保证新记忆本身不会被这次整理淘汰
    with db.write() as s:
        mid = m2._exec(s, "INSERT INTO agent_memory(kind,content,norm,status,source,weight,hits,merged_from,"
                          "created_at,updated_at) VALUES(:k,:c,:no,'active',:so,1,0,'[]',:n,:n)",
                       k=kind, c=content, no=norm, so=source, n=now)
    return get(db, int(mid)) or {}, False


def upsert_with_rule(db: Any, kind: str, content: str, rule_spec: dict[str, Any], *,
                     source: str = "manual") -> tuple[dict, bool, dict]:
    """写记忆 + 写它的执行规则。rule_spec 须已经过 user_rules.validate。

    记忆被并入已有记忆时，规则挂到幸存的那条记忆上（删那条记忆时一起删）。
    规则描述取记忆正文，决策的 route.why 里就会显示"命中规则 #3：辅导员发的讲座不建待办"。
    """
    item, merged = upsert(db, kind, content, source=source)
    rule = user_rules.create(db, rule_spec, source="memory", memory_id=int(item["id"]),
                             note=str(item.get("content") or ""))
    return item, merged, rule


def delete(db: Any, memory_id: int) -> bool:
    """用户删除：硬删，并连带删掉合并进它的留档副本——删了就不能再以任何形式影响输出。

    同一事务里删掉这些记忆带出的执行规则（能加就能撤）。
    """
    rules_n = 0
    with db.write() as s:
        linked = [memory_id] + [int(r[0]) for r in s.execute(
            sql("SELECT id FROM agent_memory WHERE merged_into=:i"), {"i": memory_id})]
        n = s.execute(sql("DELETE FROM agent_memory WHERE id=:i"), {"i": memory_id}).rowcount
        if n:
            s.execute(sql("DELETE FROM agent_memory WHERE merged_into=:i"), {"i": memory_id})
            rules_n = user_rules.delete_by_memory(s, linked)
    if n:
        log.info("记忆已删除：id=%s，连带规则 %d 条", memory_id, rules_n)
    return bool(n)


def mark_turn(db: Any, chat_id: int, memory_id: int) -> None:
    """记下"这一轮对话被提炼成了哪条记忆"；记忆被删后，该轮原文也不再进历史。"""
    with db.write() as s:
        s.execute(sql("INSERT OR IGNORE INTO agent_memory_turns(chat_id, memory_id, created_at) "
                      "VALUES(:c, :m, :n)"), {"c": chat_id, "m": memory_id, "n": m2.now_iso(db)})


def capture_from_chat(db: Any, text: str, chat_id: int) -> int | None:
    found = extract(text)
    if not found:
        return None
    if found["kind"] not in CAPTURE_KINDS:
        log.info("跳过 %s 类记忆（事实类内容今后只走画像）长度=%d",
                 found["kind"], len(found["content"]))
        return None
    item, merged = upsert(db, found["kind"], found["content"], source="chat")
    if not item:
        return None
    mark_turn(db, chat_id, int(item["id"]))
    log.info("从对话提取记忆：kind=%s 长度=%d %s", found["kind"], len(found["content"]),
             "并入已有" if merged else "新建")
    return int(item["id"])


# ------------------------------------------------------------------ 注入
def _line(r: dict) -> str:
    return f"- [{KIND_LABEL.get(r['kind'], r['kind'])}] {r['content']}"


def _relevance(qb: set[str], norm: str) -> float:
    mb = _bigrams(norm)
    if not mb or not qb:
        return 0.0
    return len(qb & mb) / len(mb)


def select_for_prompt(db: Any, question: str, context: str = "") -> tuple[int, list[dict]]:
    """返回 (活跃记忆总数, 选中的记忆)。选中数 ≤ INJECT_MAX_ITEMS，正文 ≤ INJECT_MAX_CHARS。"""
    rows = _live_rows(db)
    if not rows:
        return 0, []
    now = datetime.now(m2._tz(db))
    qb, cb = _bigrams(normalize(question)), _bigrams(normalize(context))
    active_total = sum(1 for r in rows if r["status"] == "active")
    scored = []
    for r in rows:
        rel = max(_relevance(qb, r["norm"]), CONTEXT_FACTOR * _relevance(cb, r["norm"]))
        if r["status"] == "archived" and rel < RELEVANT:
            continue  # 归档的只在"正好用得上"时召回
        score = (KIND_RANK.get(r["kind"], 0) + RELEVANCE_WEIGHT * rel
                 + 0.2 * min(r["weight"] + r["hits"], 5)
                 + 1.0 / (1.0 + _idle_days(r, now) / RECENCY_DAYS))
        scored.append((score, int(r["id"]), r, rel))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked: list[dict] = []
    used = 0
    for _score, _id, r, rel in scored:
        cost = len(_line(r)) + 1
        if used + cost > INJECT_MAX_CHARS:
            continue  # 放不下就试下一条更短的
        picked.append({**r, "relevance": rel})
        used += cost
        if len(picked) >= INJECT_MAX_ITEMS:
            break
    _touch(db, [int(p["id"]) for p in picked if p["relevance"] >= RELEVANT])
    return active_total, picked


def _touch(db: Any, ids: list[int]) -> None:
    """相关命中：计数 +1、刷新最近命中；归档的被召回即恢复活跃。"""
    if not ids:
        return
    ph = ",".join(str(int(i)) for i in ids)
    with db.write() as s:
        s.execute(sql(f"UPDATE agent_memory SET hits=hits+1, last_hit_at=:n, status='active', archived_at=NULL "
                      f"WHERE id IN ({ph})"), {"n": m2.now_iso(db)})


def memory_lines(db: Any, question: str, context: str = "") -> list[str]:
    total, picked = select_for_prompt(db, question, context)
    if not picked:
        return []
    shown = len(picked)
    head = "【用户记忆】用户本人定下的规则/偏好与事实；判断该不该建待办、要不要提醒时照此执行"
    head += (f"（共 {total} 条，只列出与本次最相关的 {shown} 条）" if shown < total else f"（共 {shown} 条）")
    return [head, *(_line(p) for p in picked)]


# ------------------------------------------------------------------ 整理
def slim(db: Any) -> dict[str, Any]:
    """先合并 → 再归档 → 最后才删。规则永不删除：被合并的规则留档为 merged，超额的规则只归档。"""
    now = datetime.now(m2._tz(db))
    stamp = m2.now_iso(db)
    before = count(db)

    # 1. 合并：按幸存者优先级从高到低，只和"已保留的"比，避免 A~B~C 链式误并
    rows = sorted(_live_rows(db), key=_priority, reverse=True)
    kept: list[dict] = []
    merges: list[tuple[int, dict]] = []
    for r in rows:
        t = next((k for k in kept if _compatible(k["kind"], r["kind"]) and similar(k["norm"], r["norm"])), None)
        if t is None:
            kept.append(r)
            continue
        t["weight"] += r["weight"]
        t["hits"] += r["hits"]
        t["merged_from"] = [*t["merged_from"], int(r["id"]), *r["merged_from"]]
        t["last_hit_at"] = _latest_stamp(t.get("last_hit_at"), r.get("last_hit_at"))
        if r["status"] == "active" and t["status"] != "active":
            t["status"], t["archived_at"] = "active", None
        t["_dirty"] = True
        merges.append((int(t["id"]), r))

    # 2. 归档（不减条数，只减注入候选）
    archived = 0

    def archive(k: dict) -> None:
        nonlocal archived
        k["status"], k["archived_at"], k["_dirty"] = "archived", stamp, True
        archived += 1

    nonrule = [k for k in kept if k["kind"] != "rule"]
    for k in nonrule:
        if k["status"] == "active" and _idle_days(k, now) > IDLE_ARCHIVE_DAYS:
            archive(k)
    active_nr = sorted((k for k in nonrule if k["status"] == "active"), key=lambda k: (_value(k, now), k["id"]))
    for k in active_nr[: max(0, len(active_nr) - NONRULE_ACTIVE_MAX)]:
        archive(k)
    active_rules = sorted((k for k in kept if k["kind"] == "rule" and k["status"] == "active"),
                          key=lambda k: (-_idle_days(k, now), k["id"]))
    for k in active_rules[: max(0, len(active_rules) - RULE_ACTIVE_MAX)]:
        archive(k)

    # 3. 删除（只删偏好/事实）
    doomed: set[int] = set()
    for k in nonrule:
        if k["status"] == "archived" and _age_days(k.get("archived_at"), now) > ARCHIVE_TTL_DAYS:
            doomed.add(int(k["id"]))
    total = before - sum(1 for _, r in merges if r["kind"] != "rule") - len(doomed)
    if total > TOTAL_MAX:
        pool = sorted((k for k in nonrule if int(k["id"]) not in doomed),
                      key=lambda k: (k["status"] == "active", KIND_RANK[k["kind"]], _value(k, now), k["id"]))
        for k in pool:
            if total <= TOTAL_MAX:
                break
            doomed.add(int(k["id"]))
            total -= 1

    with db.write() as s:
        for tid, r in merges:
            if r["kind"] == "rule":
                s.execute(sql("UPDATE agent_memory SET status='merged', merged_into=:t, "
                              "archived_at=COALESCE(archived_at, :n) WHERE id=:i"),
                          {"t": tid, "n": stamp, "i": r["id"]})
            else:
                s.execute(sql("DELETE FROM agent_memory WHERE id=:i"), {"i": r["id"]})
            s.execute(sql("UPDATE agent_memory SET merged_into=:t WHERE merged_into=:i"), {"t": tid, "i": r["id"]})
            s.execute(sql("UPDATE agent_memory_turns SET memory_id=:t WHERE memory_id=:i"),
                      {"t": tid, "i": r["id"]})
            user_rules.repoint(s, int(r["id"]), tid)  # 规则跟着挂到幸存者
        for k in kept:
            if int(k["id"]) in doomed or not k.get("_dirty"):
                continue
            s.execute(sql("UPDATE agent_memory SET weight=:w, hits=:h, merged_from=:mf, last_hit_at=:lh, "
                          "status=:st, archived_at=:aa WHERE id=:i"),
                      {"w": k["weight"], "h": k["hits"], "mf": json.dumps(k["merged_from"]),
                       "lh": k.get("last_hit_at"), "st": k["status"], "aa": k.get("archived_at"), "i": k["id"]})
        for i in doomed:
            s.execute(sql("DELETE FROM agent_memory WHERE id=:i"), {"i": i})

    after = count(db)
    rules = int(m2._rows(db, "SELECT COUNT(*) n FROM agent_memory WHERE kind='rule'")[0]["n"])
    log.info("记忆整理：%d → %d（合并 %d，归档 %d，删除 %d）", before, after, len(merges), archived, len(doomed))
    return {"before": before, "after": after, "merged": len(merges), "archived": archived,
            "deleted": len(doomed), "rules": rules, "rules_deleted": 0}
