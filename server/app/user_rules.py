""" / 用户规则层 —— 记忆是给人看的，规则是给系统执行的。

链路：模型感知（kind / audience_type，路由表照常算出原判）→ 本模块 → 落库。
命中任何一条 active 规则时，整条消息（消息块）的档位改成规则的 action，
route 写明命中哪条规则；模型原判整份保留在 route.model 里，方便以后对比"规则和模型谁更对"。
没有 active 规则或没有命中时原样返回，路由表的结果一个字都不动。

匹配（全部满足才算命中，至少一个字段）：
- group_id：精确等于消息块所在群
- sender  ：包含在发送者名称里（群名片、昵称都算）
- keyword ：包含在消息块正文里
比较前双方都做 NFKC + casefold（全角/半角、大小写不敏感），除此之外没有任何模糊匹配。

优先级：匹配字段数降序，相同时 id 升序。
status 只有 'active' 参与判定；'suggested'（以及以后可能出现的其它状态）一律不生效。
对话确认卡［恢复］写入 'disabled'（停用），同样不参与判定，行保留可在规则页重开。

对话产生的建议走 suggest_from_chat —— 状态固定为 suggested（模型给什么 status 都不认），
source='chat'，note 存用户原话（≤60 字）。与已有规则（任意状态）条件+动作相同则沿用已有那条、不插入。
confirm 把规则改成 active（幂等）；reject 直接删除。

日志只记 id、来源、状态、条件个数，不记规则内容（属于用户隐私）。
"""

from __future__ import annotations

import json
import logging
import unicodedata
from typing import Any

from sqlalchemy import text as sql

from app import m2

log = logging.getLogger("app.user_rules")

FIELDS = ("group_id", "sender", "keyword")
ACTIONS = ("ignored",)  # 规则只决定"看不看"，不决定进哪一档
JUDGMENT_ACTIONS = ("todo", "review")  # 已退役，模型再提就丢弃
JUDGMENT_DROP_WHY = ("规则现在只管「不看」；进待办还是待确认只看消息本身（做什么、谁来做、什么时候）。"
                     "如果是因为你的身份，告诉我你是谁，我写进「关于我」")
STATUSES = ("active", "suggested")  # 接口只接受这两个；判定只认 active
TEXT_MAX = 50
NOTE_MAX = 80
CHAT_NOTE_MAX = 60  # 对话建议的 note 存用户原话，截 60 字
SENDER_KEYS = ("sender_name", "card", "nickname", "sender_card", "sender_nickname")
RAW_KEYS = ("raw", "raw_event", "raw_json", "event", "payload")
COLS = "id, group_id, sender, keyword, action, status, source, memory_id, note, hits, created_at"


# ------------------------------------------------------------------ 校验
def validate(body: Any, *, what: str = "保存规则失败") -> dict[str, Any]:
    """校验 {"match": {...}, "action": ..., "status": ..., "note": ...}，返回规范化的 spec。

    不认识的 match 字段直接 400：静默丢掉会让规则比用户以为的更宽（少了一个条件）。
    """
    def bad(msg: str) -> Exception:
        return m2._bad(f"{what}：{msg}")

    if not isinstance(body, dict):
        raise bad("规则必须是 JSON 对象")
    match = body.get("match")
    if not isinstance(match, dict):
        raise bad("match 必须是对象，且至少包含 group_id / sender / keyword 之一")
    unknown = sorted(str(k) for k in match if k not in FIELDS)
    if unknown:
        raise bad(f"match 只支持 group_id / sender / keyword，不认识：{'、'.join(unknown)}")
    spec: dict[str, Any] = {"group_id": None, "sender": None, "keyword": None}
    g = match.get("group_id")
    if g is not None:
        if isinstance(g, bool):
            raise bad("group_id 必须是群号数字")
        if isinstance(g, int):
            spec["group_id"] = g
        elif isinstance(g, str) and g.strip().isdigit():
            spec["group_id"] = int(g.strip())
        else:
            raise bad("group_id 必须是群号数字")
    for k in ("sender", "keyword"):
        v = match.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            raise bad(f"{k} 必须是非空字符串")
        v = v.strip()
        if len(v) > TEXT_MAX:
            raise bad(f"{k} 不能超过 {TEXT_MAX} 字")
        spec[k] = v
    if all(spec[k] is None for k in FIELDS):
        raise bad("match 至少要有一个字段")
    action = body.get("action")
    if action not in ACTIONS:
        raise bad("action 只能是 ignored（不看）；进待办还是待确认由消息本身决定，规则不再指定")
        raise bad("action 只能是 todo / review / ignored")
    status = body.get("status")
    if status is None:
        status = "active"
    if status not in STATUSES:
        raise bad("status 只能是 active / suggested")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise bad("note 必须是字符串")
    note = " ".join((note or "").split())[:NOTE_MAX]
    return {**spec, "action": action, "status": status, "note": note}


# ------------------------------------------------------------------ 存取
def _out(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "match": {k: r[k] for k in FIELDS if r.get(k) not in (None, "")},
        "action": r["action"],
        "status": r["status"],
        "source": r.get("source") or "manual",
        "memory_id": r.get("memory_id"),
        "hits": int(r.get("hits") or 0),
        "created_at": r.get("created_at"),
        "note": r.get("note") or "",
    }


def get(db: Any, rule_id: int) -> dict | None:
    rows = m2._rows(db, f"SELECT {COLS} FROM user_rules WHERE id=:i", i=rule_id)
    return _out(rows[0]) if rows else None


def list_all(db: Any) -> list[dict]:
    """生效的在前，其余（suggested 等）在后；同组内按 id 升序（也就是优先级的同分顺序）。"""
    rows = m2._rows(db, f"SELECT {COLS} FROM user_rules "
                        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id")
    return [_out(r) for r in rows]


def create(db: Any, spec: dict[str, Any], *, source: str = "manual", memory_id: int | None = None,
           note: str | None = None) -> dict:
    """写一条规则。来自记忆时，同一条记忆上完全相同的规则只保留一条（重复保存不重复建）。"""
    text_note = spec.get("note") or " ".join(str(note or "").split())[:NOTE_MAX]
    now = m2.now_iso(db)
    params = {"g": spec.get("group_id"), "se": spec.get("sender"), "k": spec.get("keyword"),
              "a": spec["action"], "st": spec.get("status") or "active"}
    rid: int | None = None
    with db.write() as s:
        if memory_id is not None:
            row = s.execute(sql("SELECT id FROM user_rules WHERE memory_id=:m AND group_id IS :g "
                                "AND sender IS :se AND keyword IS :k AND action=:a ORDER BY id LIMIT 1"),
                            {**params, "m": memory_id}).first()
            if row is not None:
                rid = int(row[0])
                s.execute(sql("UPDATE user_rules SET status=:st WHERE id=:i"), {"st": params["st"], "i": rid})
        if rid is None:
            rid = m2._exec(s, "INSERT INTO user_rules(group_id,sender,keyword,action,status,source,memory_id,"
                              "note,hits,created_at) VALUES(:g,:se,:k,:a,:st,:so,:m,:no,0,:n)",
                           **params, so=source, m=memory_id, no=text_note, n=now)
    n_fields = sum(1 for k in FIELDS if spec.get(k) is not None)
    log.info("用户规则已保存：id=%s source=%s status=%s 条件数=%d", rid, source, params["st"], n_fields)
    return get(db, int(rid)) or {}


def delete(db: Any, rule_id: int) -> bool:
    with db.write() as s:
        n = s.execute(sql("DELETE FROM user_rules WHERE id=:i"), {"i": rule_id}).rowcount
    if n:
        log.info("用户规则已删除：id=%s", rule_id)
    return bool(n)


# ------------------------------------------------------------------  对话建议
def _dedup_key(r: dict) -> tuple[Any, ...]:
    """去重键：(group_id, sender, keyword, action)，文本字段按匹配同样的口径折叠。

    匹配时双方都做 NFKC + casefold，所以"讲座"与"讲座"（全角）在判定上就是同一条规则，
    去重也必须把它们当成同一条，否则会出现两条判定效果完全一样的规则。
    """
    g = r.get("group_id")
    try:
        g = int(g) if g not in (None, "") else None
    except (TypeError, ValueError):
        g = str(g)
    se = _fold(r.get("sender")).strip() or None
    kw = _fold(r.get("keyword")).strip() or None
    return (g, se, kw, r.get("action"))




def _match_key(r: dict) -> tuple[Any, ...]:
    """同键 = 匹配条件 (group_id, sender, keyword) 相同，不含 action。折叠口径与 _dedup_key 一致。"""
    return _dedup_key(r)[:3]


def upsert_from_chat(db: Any, raw: Any, user_text: str) -> tuple[dict | None, str, str]:
    """把模型给的 {match, action} 落库。返回 (规则, outcome, 丢弃原因)。

    只接受 action=ignored；todo/review 直接丢弃（不写库、不出卡片），
    丢弃原因会进助手回复的结果行，避免用户以为"它没听清"。
    outcome：created / updated / unchanged / dropped（含义同前）
    """
    if not isinstance(raw, dict):
        log.info("规则建议被忽略：不是对象")
        return None, "dropped", "规则格式不对"
    if raw.get("action") in JUDGMENT_ACTIONS:
        log.info("规则建议被丢弃：判定型动作 %s 已退役", raw.get("action"))
        return None, "dropped", JUDGMENT_DROP_WHY
    body = {"match": raw.get("match"), "action": raw.get("action"),
            "status": "suggested", "note": _chat_note(user_text)}
    try:
        spec = validate(body, what="规则建议")
    except Exception:
        log.info("规则建议被忽略：字段不合法")
        return None, "dropped", "规则的条件或动作不合法"
    key = _match_key(spec)
    now = m2.now_iso(db)
    outcome = "unchanged"
    with db.write() as s:
        rows = [dict(r._mapping) for r in s.execute(sql(
            "SELECT id, group_id, sender, keyword, action, status FROM user_rules ORDER BY id"))]
        same = [r for r in rows if _match_key(r) == key]
        if not same:
            rid = m2._exec(s, "INSERT INTO user_rules(group_id,sender,keyword,action,status,source,memory_id,"
                              "note,hits,created_at) VALUES(:g,:se,:k,:a,'suggested','chat',NULL,:no,0,:n)",
                           g=spec["group_id"], se=spec["sender"], k=spec["keyword"], a=spec["action"],
                           no=spec["note"], n=now)
            outcome = "created"
        else:
            target = same[0]
            rid = int(target["id"])
            if len(same) > 1:
                log.warning("同匹配条件的规则有 %d 条（历史遗留），只更新最早的 #%s", len(same), rid)
            if target["action"] != spec["action"]:  # 只可能是迁移前残留的判定型规则
                s.execute(sql("UPDATE user_rules SET action=:a, note=:no, status='suggested' WHERE id=:i"),
                          {"a": spec["action"], "no": spec["note"], "i": rid})
                outcome = "updated"
    log.info("规则建议：id=%s outcome=%s", rid, outcome)
    return get(db, int(rid)), outcome, ""


def _chat_note(text: Any) -> str:
    return " ".join(str(text or "").split())[:CHAT_NOTE_MAX]


def suggest_from_chat(db: Any, raw: Any, user_text: str) -> tuple[dict | None, bool]:
    """旧签名保留，给其它调用方用。口径已改：同匹配条件 → 更新那条（见 upsert_from_chat）。"""
    rule, outcome, _why = upsert_from_chat(db, raw, user_text)
    return rule, outcome == "created"


def confirm(db: Any, rule_id: int) -> dict | None:
    """suggested → active；已是 active 幂等成功。不存在 → None。"""
    with db.write() as s:
        row = s.execute(sql("SELECT status FROM user_rules WHERE id=:i"), {"i": rule_id}).first()
        if row is None:
            return None
        changed = row[0] != "active"
        if changed:
            s.execute(sql("UPDATE user_rules SET status='active' WHERE id=:i"), {"i": rule_id})
    log.info("用户规则确认：id=%s %s", rule_id, "已生效" if changed else "原本就是 active")
    return get(db, rule_id)


def reject(db: Any, rule_id: int) -> bool:
    """拒绝 = 删除（无论状态）。"""
    return delete(db, rule_id)


RULE_OFF_STATUS = "disabled"  # 停用：不参与判定（find 只认 active），规则页仍能看到


def disable_from_chat(db: Any, rid: int) -> tuple[dict | None, str]:
    """对话确认卡［恢复］→ 停用一条「不看」。返回 (停用后的规则, off/absent/failed)。

    只改状态，不删行：用户以后在规则页还能看到它、也能重新确认生效。
    """
    before = get(db, rid)
    if before is None or before.get("status") not in STATUSES:
        return before, "absent"
    with db.write() as s:
        s.execute(sql("UPDATE user_rules SET status=:st WHERE id=:i AND status IN ('active','suggested')"),
                  {"st": RULE_OFF_STATUS, "i": rid})
    after = get(db, rid)
    log.info("用户规则停用：id=%s → %s", rid, (after or {}).get("status"))
    return after, ("off" if after is not None and after.get("status") == RULE_OFF_STATUS else "failed")


def _has_table(s: Any) -> bool:
    return s.execute(sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_rules'")).first() is not None


def delete_by_memory(s: Any, memory_ids: list[int]) -> int:
    """在调用方的事务里删掉这些记忆带出的规则（删记忆的级联）。返回删除条数。"""
    if not memory_ids or not _has_table(s):
        return 0
    ph = ",".join(str(int(i)) for i in memory_ids)
    return int(s.execute(sql(f"DELETE FROM user_rules WHERE memory_id IN ({ph})")).rowcount or 0)


def repoint(s: Any, old_memory_id: int, new_memory_id: int) -> None:
    """记忆被合并时，规则跟着改挂到幸存的那条记忆上（在调用方事务里执行）。"""
    if _has_table(s):
        s.execute(sql("UPDATE user_rules SET memory_id=:n WHERE memory_id=:o"),
                  {"n": new_memory_id, "o": old_memory_id})


# ------------------------------------------------------------------ 匹配
def _fold(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).casefold()


def specificity(r: dict) -> int:
    return sum(1 for k in FIELDS if r.get(k) not in (None, ""))


def _sender_names(db: Any, block: dict, infos: list[dict]) -> list[str]:
    """发送者所有可用的名字：群名片、昵称都算（包含在群名片或昵称里）。"""
    names = {str(i.get("sender_name") or "") for i in infos}
    try:
        ids = [int(x) for x in json.loads(block.get("msg_ids") or "[]")]
    except (TypeError, ValueError):
        ids = []
    if ids:
        ph = ",".join(str(i) for i in ids)
        for row in m2._rows(db, f"SELECT * FROM messages WHERE msg_id IN ({ph})"):
            for k in SENDER_KEYS:
                v = row.get(k)
                if isinstance(v, str):
                    names.add(v)
            for k in RAW_KEYS:
                v = row.get(k)
                if not (isinstance(v, str) and v.lstrip().startswith("{")):
                    continue
                try:
                    ev = json.loads(v)
                except ValueError:
                    continue
                snd = ev.get("sender") if isinstance(ev, dict) else None
                if isinstance(snd, dict):
                    for kk in ("card", "nickname"):
                        if isinstance(snd.get(kk), str):
                            names.add(snd[kk])
    return [n for n in (x.strip() for x in names) if n]


def _hit(r: dict, gid: Any, names: list[str], text: str) -> bool:
    if specificity(r) == 0:
        return False  # 防御：库里被手改出的空条件规则不能匹配一切
    if r.get("group_id") is not None:
        try:
            if gid is None or int(gid) != int(r["group_id"]):
                return False
        except (TypeError, ValueError):
            return False
    sender = r.get("sender")
    if sender:
        f = _fold(sender)
        if not any(f in n for n in names):
            return False
    kw = r.get("keyword")
    if kw and _fold(kw) not in text:
        return False
    return True


def find(db: Any, block: dict, infos: list[dict]) -> tuple[dict | None, list[dict]]:
    """返回 (生效的那条规则, 同时命中但没被采用的规则)。只看 status='active'。"""
    rules = m2._rows(db, "SELECT * FROM user_rules WHERE status='active' ORDER BY id")
    if not rules:
        return None, []
    text = _fold(block.get("text"))
    names = [_fold(n) for n in _sender_names(db, block, infos)]
    hits = [r for r in rules if _hit(r, block.get("group_id"), names, text)]
    if not hits:
        return None, []
    hits.sort(key=lambda r: (-specificity(r), int(r["id"])))
    return hits[0], hits[1:]


def describe(rule: dict) -> str:
    if rule.get("note"):
        return str(rule["note"])
    parts = []
    if rule.get("group_id") is not None:
        parts.append(f"群 {rule['group_id']}")
    if rule.get("sender"):
        parts.append(f"发送者含「{rule['sender']}」")
    if rule.get("keyword"):
        parts.append(f"正文含「{rule['keyword']}」")
    label = m2.VERDICT_LABELS.get(rule.get("action", ""), rule.get("action", ""))
    return f"{'、'.join(parts)} → {label}"


_SNAP_KEYS = ("stage", "verdict", "reason", "confidence", "evidence", "provider", "model",
              "item", "routed", "route")


def _is_private(block: dict, infos: list[dict]) -> bool:
    """私聊判定由程序完成，不依赖模型。"""
    if str(block.get("message_type") or "") == "private":
        return True
    if any(str((i or {}).get("message_type") or "") == "private" for i in infos or []):
        return True
    return block.get("group_id") in (None, 0, "")


def _points_at_me(r: dict) -> bool:
    """任一事项 audience_type == "me"（点名 / @ / 私聊都归这一类）。"""
    cands: list[Any] = []
    for pair in r.get("routed") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            cands.extend(pair)
    cands.append(r.get("item"))
    cands.extend(r.get("items") or [])
    return any(isinstance(x, dict) and str(x.get("audience_type") or "").strip().lower() == "me"
               for x in cands)


def _verdict_before(r: dict) -> str:
    vs = {str((rt or {}).get("verdict") or "") for pair in (r.get("routed") or [])
          if isinstance(pair, (list, tuple)) and len(pair) == 2 for rt in [pair[1]]}
    for v in ("todo", "review"):
        if v in vs:
            return v
    return str(r.get("verdict") or "")


def apply(db: Any, block: dict, infos: list[dict], r: dict) -> dict:
    """只套「不看」。没命中 → 原样返回 r。

    命中但点名本人或是私聊 → 照原判（只在路由说明里注一句）；
    否则 → 忽略，并带上 rule_hit（原判是 todo/review 时）供 process_block 写命中记录。
    """
    rule, others = find(db, block, infos)
    if rule is None or rule.get("action") != "ignored":
        return r
    rid = int(rule["id"])
    if _points_at_me(r) or _is_private(block, infos):
        reason = "私聊" if _is_private(block, infos) else "点名了你"
        log.info("命中「不看」#%s 但%s，照原判", rid, reason)
        route = r.get("route")
        if isinstance(route, dict):
            note = f"（命中「不看」#{rid}，但{reason}，照原判）"
            return {**r, "route": {**route, "why": f"{route.get('why', '')}{note}"}}
        return r
    by = f"rule#{rid}"
    why = f"命中「不看」#{rid}：{describe(rule)}"
    if others:
        why += f"（同时命中 {'、'.join(f'#{int(o['id'])}' for o in others)}）"
    vb = _verdict_before(r)
    if vb and vb != "ignored":
        why += f"；模型原判：{m2.VERDICT_LABELS.get(vb, vb)}"
    route = m2._route_info("ignored", by, why, rule_id=rid, model=r.get("route"))
    out = {**r, "verdict": "ignored", "route": route, "user_rule_id": rid, "rule_why": why,
           "routed": None, "item": None}
    if vb in ("todo", "review"):  # 原判本来就是忽略的不算"规则让你少看了"，不记
        snap = {k: r.get(k) for k in _SNAP_KEYS}
        snap["raw"] = str(r.get("raw") or "")[:8000]
        out["rule_hit"] = {"rule_id": rid, "verdict_before": vb, "snapshot": snap}
    return out


