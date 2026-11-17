"""意图 → 现有执行器 → 五态回执。

顺序：事件 → 画像 → 关注 → 不看 → 卡片（画像时间闸门的说明要知道 event_kind）。
每个执行器产出 Receipt，由 app.receipt_view 渲染成用户可见的行（chat_send / 确认卡都用它）。
created / already 必须带回读到的 row：写后没回读到的，步骤 3 会按 failed 显示。
判定侧（_judge / ROUTE_TABLE / user_rules.find/apply）一律不碰。

 步骤 4：纯 none 轮不再按触发表兜底。说话层判 user_wants_change=true 而意图层全是 none、
本轮又没有回执时，由 divergence_card 出一张 diverge 确认卡（［放进待确认］［不用］），
点［放进待确认］只把原话放进待确认（land_chat_raw_review），不建待办、不重判（产品约定）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app import chat_intent, focus_topics, m2, user_rules
from app.chat_intent import Intent, IntentResult  # noqa: F401  对外再导出

log = logging.getLogger(__name__)

STATES = ("created", "already", "pending", "failed", "unclear")  # created=本轮实际产生改动（含删除）
CARD_KINDS = ("focus_add", "focus_remove", "profile", "ignore_remove", "unclear", "diverge")
CHOICES = ("accept", "review", "dismiss")
MAX_CARDS = 5
DISMISS_LINE = "好的，没有改动"
CARDS_LOST_LINE = "刚才有 {n} 处要你确认的设置没能显示出来，这些都没有生效"
#  步骤 4：说话层与意图层对账用的确认卡（不是新的意图类型）
DIVERGE_KIND = "diverge"
DIVERGE_QUESTION = "这句话没有改动任何设置。要把原话放进待确认吗？"
PROFILE_NONE_WHY = "这句里没有能写进「关于我」的长期情况"
PROFILE_FAIL_WHY = "没整理出要写的内容"
EVENT_FAIL_WHY = "系统内部出错"
_ACCEPT_LABEL = {"focus_add": "加", "focus_remove": "移除", "profile": "记", "ignore_remove": "恢复"}


@dataclass
class Receipt:
    kind: str
    target: str
    state: str
    row: dict | None = None   # created/already 必须是写后/写前回读到的行
    why: str = ""             # failed 的原因


@dataclass
class CardSpec:
    kind: str
    question: str
    topic: str = ""
    target_id: int | None = None
    evidence: str = ""
    user_text: str = ""


@dataclass
class ExecResult:
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    pending: list[CardSpec] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    event_kind: str = ""
    extract_tried: int = 0
    extract_result: str = ""


def _fail(ex: ExecResult, kind: str, target: str, why: str) -> None:
    ex.receipts.append(Receipt(kind, target, "failed", why=why))


def _absent_row(**values: Any) -> dict:
    """不存在同名的行时，把「回读确认它不在」本身当成 row（created/already 要求带回读证据）。"""
    return {**values, "present": False}


# ---------- 卡片 ----------

def divergence_card(wants_change: bool, pure_none: bool, has_receipt: bool,
                    pending: list[CardSpec], text: str) -> CardSpec | None:
    """分歧兜底：说话层认为用户想改点什么，意图层全是 none，本轮又没有回执也没有卡 → 一张确认卡。
    只判断"这句话想要什么"，不碰已生成的待办/待确认（产品约定）。"""
    if not wants_change or not pure_none or has_receipt or pending:
        return None
    return CardSpec(DIVERGE_KIND, DIVERGE_QUESTION, user_text=text,
                    evidence="talk=change;intent=none")


def choices_for(kind: str, topic: str) -> list[tuple[str, str]]:
    if kind == DIVERGE_KIND:  # 最保守的两个选项：只进待确认，或者什么都不改
        return [("review", "放进待确认"), ("dismiss", "不用")]
    if kind == "unclear":
        return ([("accept", "加进关注")] if topic else []) + [("review", "放进待确认"), ("dismiss", "不用")]
    return [("accept", _ACCEPT_LABEL[kind]), ("dismiss", "不用")]


def card_text(question: str, choices: list[tuple[str, str]]) -> str:
    """按钮没渲染时也能读懂：问句 + 选项文字。"""
    return question + " " + "".join(f"［{label}］" for _v, label in choices)


def card_payload(db: Any, card: dict) -> dict:
    topic = card.get("topic") or ""
    # 保留期清理可能已经把那张 chat_messages 删掉了：卡片本身还在，正文给 None 而不是 500
    msg = m2._rows(db, f"SELECT {m2.CHAT_COLS} FROM chat_messages WHERE id=:i",
                   i=int(card["chat_message_id"]))
    return {"id": int(card["id"]), "kind": card["kind"], "topic": topic,
            "target_id": card.get("target_id"), "status": card["status"],
            "choices": [{"value": v, "label": lab} for v, lab in choices_for(card["kind"], topic)],
            "message": msg[0] if msg else None}


def post_cards(db: Any, pending: list[CardSpec], *, user_id: int) -> list[dict]:
    """把待确认意图落成 chat_messages(kind='confirm') + chat_confirms。逐张落库；
    有落不下的，补一条 assistant 文本说明（不静默）。"""
    out: list[dict] = []
    if len(pending) > MAX_CARDS:
        log.info("确认卡 %d 张，超过上限只出前 %d 张", len(pending), MAX_CARDS)
    lost = 0
    for c in pending[:MAX_CARDS]:
        try:
            now = m2.now_iso(db)
            text = card_text(c.question, choices_for(c.kind, c.topic))
            with db.write() as s:
                mid = m2._exec(s, "INSERT INTO chat_messages(role,kind,text,created_at) "
                                  "VALUES('assistant','confirm',:t,:n)", t=text, n=now)
                cid = m2._exec(s, "INSERT INTO chat_confirms(chat_message_id,user_message_id,kind,topic,"
                                  "target_id,evidence,user_text,status,created_at) "
                                  "VALUES(:m,:u,:k,:tp,:tg,:ev,:ut,'pending',:n)",
                               m=mid, u=user_id, k=c.kind, tp=c.topic or None, tg=c.target_id,
                               ev=c.evidence or None, ut=c.user_text, n=now)
            m2.hub.publish({"type": "chat.message", "id": mid, "role": "assistant",
                            "kind": "confirm", "confirm_id": cid})
            out.append(card_payload(db, m2._rows(db, "SELECT * FROM chat_confirms WHERE id=:i", i=cid)[0]))
        except Exception as exc:
            lost += 1
            log.warning("确认卡落库异常（%s）：%s", c.kind, type(exc).__name__)
    if lost:
        try:
            m2._post_assistant_text(db, CARDS_LOST_LINE.format(n=lost))
        except Exception as exc:
            log.warning("确认卡丢失说明落库异常：%s", type(exc).__name__)
    return out


def _focus_card(kind: str, i: Intent, text: str) -> CardSpec:
    q = f"把「{i.topic}」加进关注？" if kind == "focus_add" else f"把「{i.topic}」从关注里移除？"
    return CardSpec(kind, q, topic=i.topic, evidence=i.evidence, user_text=text)


def _profile_card(i: Intent, text: str) -> CardSpec:
    quote = (i.evidence or text.strip())[:60]
    return CardSpec("profile", f"把这句记进「关于我」？——「{quote}」", evidence=i.evidence, user_text=text)


def _unclear_card(i: Intent, text: str) -> CardSpec:
    topic = focus_topics.normalize(i.topic)
    quote = (i.evidence or text.strip())[:60]
    tail = f"要把「{topic}」加进关注，还是放进待确认？" if topic else "要放进待确认吗？"
    return CardSpec("unclear", f"这句我没拿准要做什么：「{quote}」。{tail}",
                    topic=topic, evidence=i.evidence, user_text=text)


# ---------- 执行器 ----------

def _event_state(kind: str) -> str:
    # land_chat_event 实际取值：todo / review / dup / drop / ""
    return {"todo": "created", "review": "pending", "dup": "already"}.get(kind, "failed")


async def _extract(db: Any, ctx: Any, text: str, ex: ExecResult) -> Any:
    from app import chat_llm

    ex.extract_tried = 1
    try:
        got = await m2._run_extract(db, ctx, text)
    except Exception as exc:
        log.warning("事件抽取异常：%s", type(exc).__name__)
        got = None
    ex.extract_result = "event" if isinstance(got, dict) else ("none" if got == "none" else "fail")
    log.info("事件抽取：结果=%s 原因=%s", ex.extract_result, chat_llm.last_extract_error() or "-")
    return got


def _land(db: Any, got: Any, text: str, uid: int, ex: ExecResult, *, raw_review: bool) -> None:
    why = ""
    try:
        if isinstance(got, dict):
            kind, line, row, why = m2.land_chat_event(db, got, text, chat_message_id=uid)
        elif raw_review:
            kind, line, row, why = m2.land_chat_raw_review(db, text, chat_message_id=uid)
        else:
            return
    except Exception as exc:
        log.warning("对话事件落地异常：%s", type(exc).__name__)
        ex.failed.append(("待办", EVENT_FAIL_WHY))
        _fail(ex, "event", "", EVENT_FAIL_WHY)
        return
    ex.event_kind = kind or ""
    if line:
        ex.done.append(line)
    if isinstance(got, dict):
        title = str(got.get("title") or "")
    else:  # 原话进待确认：标题由落地函数回读行带回来
        title = str((row or {}).get("title") or "")
    state = _event_state(ex.event_kind)
    if state in ("created", "already"):
        if row:
            ex.receipts.append(Receipt("event", title, state, row))
        else:
            _fail(ex, "event", title, "写完没能回读确认")
    elif state == "pending":
        ex.receipts.append(Receipt("event", title, "pending", row))
    else:
        _fail(ex, "event", title, why or "没能落地")


async def _exec_event(db: Any, ctx: Any, text: str, uid: int, ex: ExecResult) -> None:
    """event 意图（不分 high/low）：抽取 → land_chat_event（verify_event 四道核对 + route 不变）；
    抽不出 → 只进待确认。"""
    got = await _extract(db, ctx, text, ex)
    _land(db, got, text, uid, ex, raw_review=True)


async def _exec_profile(db: Any, ctx: Any, text: str, ex: ExecResult, *, event_kind: str = "") -> None:
    try:
        spec, st = await chat_intent.extract_profile(db, ctx, text)
    except Exception as exc:
        log.warning("画像抽取异常：%s", type(exc).__name__)
        spec, st = None, "fail"
    if spec is None:
        ex.failed.append(("画像", PROFILE_NONE_WHY if st == "none" else PROFILE_FAIL_WHY))
        _fail(ex, "profile", "", PROFILE_NONE_WHY if st == "none" else PROFILE_FAIL_WHY)
        return
    try:
        hits = m2._profile_one_off_hits(db, spec)
    except Exception as exc:  # 闸门出错按拒绝处理，不冒险写
        log.warning("画像时间闸门异常：%s", type(exc).__name__)
        ex.failed.append(("画像", "系统内部出错"))
        _fail(ex, "profile", "", "系统内部出错")
        return
    if hits:
        log.info("画像写入被时间闸门拒绝：命中 %d 处", len(hits))
        ex.done.append(m2._profile_gate_line(hits, event_kind))
        _fail(ex, "profile", "", m2._profile_gate_why(hits, event_kind))
        return
    try:
        written, why = m2.profile_write_from_chat(db, spec, text)
    except Exception as exc:
        log.warning("画像写入异常：%s", type(exc).__name__)
        written, why = None, "系统内部出错"
    if written is not None:
        ex.done.append("已更新「关于我」")
        m2.hub.publish({"type": "profile.updated", "history_id": written["history_id"]})
        ex.receipts.append(Receipt("profile", "", "created", written))
    else:
        ex.failed.append(("画像", why))
        _fail(ex, "profile", "", why or PROFILE_FAIL_WHY)


def _exec_focus_add(db: Any, topics: list[str], text: str, ex: ExecResult) -> None:
    try:
        results = focus_topics.add_from_chat(db, {"topics": topics}, text)
    except Exception as exc:
        log.warning("关注写入异常：%s", type(exc).__name__)
        ex.failed.append(("关注", "系统内部出错"))
        for t in topics:
            _fail(ex, "focus_add", t, "系统内部出错")
        return
    for row, outcome, why in results:
        if outcome == "created" and row:
            ex.done.append(f"「{row['topic']}」加进关注了")
            m2.hub.publish({"type": "focus.created", "focus_id": row["id"]})
            ex.receipts.append(Receipt("focus_add", row["topic"], "created", row))
        elif outcome == "unchanged" and row:
            ex.done.append(f"「{row['topic']}」本来就在关注里，没有重复添加")
            ex.receipts.append(Receipt("focus_add", row["topic"], "already", row))
        else:
            ex.failed.append(("关注", why or "没写进去"))
            _fail(ex, "focus_add", str((row or {}).get("topic") or ""), why or "没写进去")


def _exec_focus_remove(db: Any, topic: str, ex: ExecResult) -> None:
    why = "删完回读还在"
    try:
        row, st = focus_topics.remove_from_chat(db, topic)
    except Exception as exc:
        log.warning("关注删除异常：%s", type(exc).__name__)
        row, st, why = None, "failed", "系统内部出错"
    if st == "removed" and row:
        ex.done.append(f"「{row['topic']}」已从关注里移除")
        m2.hub.publish({"type": "focus.deleted", "focus_id": row["id"]})  # 与关注页删除同名事件
        ex.receipts.append(Receipt("focus_remove", row["topic"], "created", row))  # row 留给步骤 3 的 [撤销]
    elif st == "absent":
        ex.done.append(f"关注里没有「{topic}」，没有改动")
        ex.receipts.append(Receipt("focus_remove", topic, "already", _absent_row(topic=topic)))
    else:
        ex.failed.append(("关注", f"「{topic}」没删掉，可以去关注页手动删"))
        _fail(ex, "focus_remove", topic, why)


def _exec_ignore_add(db: Any, topic: str, text: str, ex: ExecResult) -> None:
    """只建 suggested，不生效；只支持关键词（用户已确认，不加 scope）。"""
    raw = {"match": {"keyword": topic}, "action": "ignored"}  # 与旧 RULE 标记的 match 结构一致
    try:
        rule, outcome, why = user_rules.upsert_from_chat(db, raw, text)
    except Exception as exc:
        log.warning("规则建议落库异常：%s", type(exc).__name__)
        rule, outcome, why = None, "dropped", "系统内部出错"
    if rule is not None:
        ex.done.append(m2._rule_result_line(rule, outcome))
        # 新建/更新的规则是 suggested（等确认）；本来就有且已生效的才是 already
        state = "pending" if outcome in ("created", "updated") or rule.get("status") == "suggested" else "already"
        ex.receipts.append(Receipt("ignore_add", topic, state, rule))
    else:
        ex.failed.append(("规则", why))
        _fail(ex, "ignore_add", topic, why or "没写进去")


def _exec_ignore_remove(db: Any, topic: str, text: str, ex: ExecResult) -> None:
    """只出确认卡，点了才停用；按关键词精确比对（_fold），不做模糊匹配。"""
    key = user_rules._fold(topic)
    hits = [r for r in chat_intent.ignore_rules(db)
            if str(r.get("keyword") or "").strip() and user_rules._fold(r["keyword"]) == key]
    if not hits:
        ex.done.append(f"「不看」里没有「{topic}」，没有改动")
        ex.receipts.append(Receipt("ignore_remove", topic, "already", _absent_row(topic=topic)))
        return
    for r in hits:
        rid, kw = int(r["id"]), str(r["keyword"])
        if r.get("status") == "active":
            q = f"恢复看「{kw}」相关消息？（规则 #{rid}：{user_rules.describe(r)}）"
        else:
            q = f"撤掉「不看 {kw}」这条建议？（规则 #{rid}：{user_rules.describe(r)}，还没生效）"
        ex.pending.append(CardSpec("ignore_remove", q, topic=kw, target_id=rid, user_text=text))
        ex.receipts.append(Receipt("ignore_remove", kw, "pending", r))


def _rule_keyword(rule: dict | None) -> str:
    """user_rules.get() 返回的是对外形状（match 里套着 keyword），不是库里那行。"""
    if not rule:
        return ""
    kw = rule.get("keyword")
    if kw:
        return str(kw)
    return str((rule.get("match") or {}).get("keyword") or "")


def _exec_rule_disable(db: Any, rid: int, ex: ExecResult) -> None:
    try:
        rule, st = user_rules.disable_from_chat(db, rid)
    except Exception as exc:
        log.warning("规则停用异常：%s", type(exc).__name__)
        rule, st = None, "failed"
    kw = _rule_keyword(rule)
    if st == "off":
        ex.done.append(f"已恢复看「{kw}」相关消息（规则 #{rid} 已停用）")
        ex.receipts.append(Receipt("ignore_remove", kw, "created", rule))
    elif st == "absent":
        ex.done.append(f"规则 #{rid} 已经不在生效，没有改动")
        if rule is not None:
            ex.receipts.append(Receipt("ignore_remove", kw, "already", rule))
        else:
            _fail(ex, "ignore_remove", kw, "没能确认它现在的状态")
    else:
        ex.failed.append(("规则", f"规则 #{rid} 没停用成功，可以去规则页手动停用"))
        _fail(ex, "ignore_remove", kw, "没停用成功")


# ---------- 入口 ----------

async def run(db: Any, ctx: Any, text: str, uid: int, res: IntentResult) -> ExecResult:
    ex = ExecResult()
    if res.status == "unavailable":
        ex.receipts.append(Receipt("intent_down", "", "failed", why="意图服务暂时不可用"))
        return ex
    if res.pure_none:
        #  步骤 4：纯 none 轮不再靠触发表兜底（旧门控与静默提示行已退役）。
        # 说话层与意图层对账在 chat_send 里做：有分歧就出卡交给用户。
        return ex

    by: dict[str, list[Intent]] = {k: [] for k in chat_intent.KINDS}
    for i in res.intents:
        by[i.kind].append(i)

    if by["event"]:  # 一句只抽一次事件，与旧 EVENT 标记口径一致
        await _exec_event(db, ctx, text, uid, ex)
    if by["profile"]:
        if any(i.high for i in by["profile"]):
            await _exec_profile(db, ctx, text, ex, event_kind=ex.event_kind)
        else:
            ex.pending.append(_profile_card(by["profile"][0], text))
    hi_add = [i.topic for i in by["focus_add"] if i.high]
    if hi_add:
        _exec_focus_add(db, hi_add, text, ex)
    ex.pending.extend(_focus_card("focus_add", i, text) for i in by["focus_add"] if not i.high)
    for i in by["focus_remove"]:
        if i.high:
            _exec_focus_remove(db, i.topic, ex)
        else:
            ex.pending.append(_focus_card("focus_remove", i, text))
    for i in by["ignore_add"]:
        _exec_ignore_add(db, i.topic, text, ex)
    for i in by["ignore_remove"]:
        _exec_ignore_remove(db, i.topic, text, ex)
    if by["unclear"]:  # 一轮只问一次
        ex.pending.append(_unclear_card(by["unclear"][0], text))
        ex.receipts.append(Receipt("unclear", by["unclear"][0].topic, "unclear"))
    return ex


async def resolve(db: Any, ctx: Any, card: dict, choice: str) -> ExecResult:
    """确认卡被点击后执行（调用方已用 UPDATE…WHERE status='pending' 抢占，保证只执行一次）。"""
    ex = ExecResult()
    kind, topic = card["kind"], card.get("topic") or ""
    text, uid = str(card["user_text"]), int(card["user_message_id"])
    if choice == "dismiss":
        ex.done.append(DISMISS_LINE)
        return ex
    if kind == "focus_add" or (kind == "unclear" and choice == "accept"):
        _exec_focus_add(db, [topic], text, ex)
    elif kind == "focus_remove":
        _exec_focus_remove(db, topic, ex)
    elif kind == "profile":
        await _exec_profile(db, ctx, text, ex)
    elif kind == "ignore_remove":
        _exec_rule_disable(db, int(card["target_id"]), ex)
    elif kind in ("unclear", DIVERGE_KIND) and choice == "review":
        _land(db, None, text, uid, ex, raw_review=True)  # 只进待确认，不建待办
    if not ex.done and not ex.failed:
        ex.done.append(DISMISS_LINE)
    return ex
