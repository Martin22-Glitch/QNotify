""" 意图层：判断用户这一句想让系统做什么（封闭分类），以及画像载荷抽取。

只判断，不写库；写库由 intent_exec 负责。
模型调用走 chat_llm.json_call：与对话共用"对话"模型配置，思考强制关闭，账本 purpose 单独记。
意图调用不带对话历史（用户已确认），只带三份清单。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app import chat_llm, event_route, focus_topics, m2, user_rules

log = logging.getLogger(__name__)

KINDS: tuple[str, ...] = ("focus_add", "focus_remove", "ignore_add", "ignore_remove",
                          "profile", "event", "none", "unclear")
CONFIDENCE: tuple[str, ...] = ("high", "low")
NEEDS_TOPIC = frozenset({"focus_add", "focus_remove", "ignore_add", "ignore_remove"})
ITEM_KEYS = frozenset({"kind", "topic", "evidence", "confidence"})
MAX_INTENTS = 5
LIST_CAP = 50
FREE_TOPIC_MAX = 40
EVIDENCE_MAX = 200
USER_TEXT_MAX = 500
ABOUT_HINT_MAX = 300
PURPOSE_INTENT = "intent"
PURPOSE_PROFILE = "intent_profile"

INTENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["intents"],
    "properties": {"intents": {
        "type": "array", "minItems": 1, "maxItems": MAX_INTENTS,
        "items": {"type": "object", "additionalProperties": False,
                  "required": ["kind", "topic", "evidence", "confidence"],
                  "properties": {"kind": {"enum": list(KINDS)}, "topic": {"type": "string"},
                                 "evidence": {"type": "string"},
                                 "confidence": {"enum": list(CONFIDENCE)}}}}},
}

INTENT_SYSTEM = """你是分类器，不聊天。判断用户这一句话想让系统做什么，只输出 JSON：
{"intents":[{"kind":"…","topic":"…","evidence":"…","confidence":"high|low"}]}
必须至少输出一项；什么都不用做时输出 {"intents":[{"kind":"none","topic":"","evidence":"","confidence":"high"}]}。

kind 只能是：
- focus_add：想让某类消息更被留意（留意 / 关注 / 别漏 / 盯着 …）。topic=话题词，≤20字，用原话里的词
- focus_remove：某个关注不要了。topic 必须逐字照抄下方「当前关注」里的某一项
- ignore_add：某类消息以后不看 / 不用提醒 / 别推给我。topic=要不看的关键词
- ignore_remove：之前不看的现在又要看了。topic 必须逐字照抄下方「当前不看」里括号前的关键词
- profile：讲自己长期的身份、职责、在做的事（年级、专业、班委、备考…）
- event：自己要做的一件具体事（交材料、考试、报名…），或让你帮忙记一件事
- none：闲聊、提问、情绪，不需要改任何东西
- unclear：像是想改设置或记事，但拿不准是哪一种
evidence：用户原话里逐字出现的片段。topic 对 profile/event/none/unclear 可为空字符串。
拿不准时 confidence 写 low，不要硬猜成 high。一句话里有几件事就输出几项。"""

PROFILE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["about", "evidence"],
    "properties": {"about": {"type": ["string", "null"]},
                   "evidence": {"type": "array", "items": {"type": "string"}}},
}

# 内容规则改写自原 PROFILE_MARKER_GUIDE，只是输出从标记改为 JSON
PROFILE_SYSTEM = """你只做一件事：把用户这句话里关于他自己的长期情况，合并进他现有的「关于我」。只输出 JSON：
{"about": "合并后的完整新版本", "evidence": ["原话片段"]}
这句话里没有可写的长期情况时输出 {"about": null, "evidence": []}。
规则：
- about 必须在现有「关于我」基础上合并，给出完整新版本；不要只写新增的一句，也不要丢掉原有内容。第一人称，不超过 500 字。
- 只写身份事实：年级、专业、班委职务、长期在做的事、在备考什么等。周期性习惯（每周三晚上有课、周末回家、十点以后不看消息）可以写。
- about 里不得写一次性的日期或时间（今天、明天、下周五、下个月、12月7号、月底、今年、2026年等）。
- evidence 每一项必须是用户这句话里逐字出现的片段，至少 4 个字。"""


@dataclass(frozen=True)
class Intent:
    kind: str
    topic: str = ""
    evidence: str = ""
    confidence: str = "high"

    @property
    def high(self) -> bool:
        return self.confidence == "high"


@dataclass
class IntentResult:
    intents: list[Intent]
    status: str            # ok / retry_ok / unclear / unavailable
    attempts: int = 0
    error: str = ""

    @property
    def kinds(self) -> str:
        return ",".join(i.kind for i in self.intents)

    @property
    def pure_none(self) -> bool:
        """意图层正常给出结果，且只有 none（045C 门控只在这种轮次兜底）。"""
        return self.status in ("ok", "retry_ok") and all(i.kind == "none" for i in self.intents)


class SchemaError(ValueError):
    pass


def unclear_result(text: str, error: str, attempts: int = 0) -> IntentResult:
    """解析两次都失败 / 调用异常：记成 unclear（出一键确认卡），绝不记成 none。"""
    return IntentResult([Intent("unclear", "", str(text or "").strip()[:EVIDENCE_MAX], "low")],
                        status="unclear", attempts=attempts, error=error)


def _squash(s: str) -> str:
    return event_route._squash(s)  # 与 verify_event 同一"原话子串"口径


def _load_json(raw: str) -> Any:
    try:
        return json.loads(chat_llm._FENCE_STRIP_RE.sub("", raw.strip()))
    except (ValueError, TypeError):
        raise SchemaError("不是 JSON") from None


def ignore_rules(db: Any) -> list[dict]:
    return m2._rows(db, "SELECT * FROM user_rules WHERE action='ignored' "
                        "AND status IN ('active','suggested') ORDER BY id")


def _rule_label(r: dict) -> str:
    kw = str(r.get("keyword") or "").strip()
    desc = user_rules.describe(r)
    if kw:
        return f"{kw}（#{int(r['id'])} {desc}）"
    return f"（#{int(r['id'])} {desc}；不是关键词规则，只能在规则页改）"


def build_user(db: Any, text: str) -> str:
    focus = [r["topic"] for r in focus_topics.list_topics(db)][:LIST_CAP]
    rules = [_rule_label(r) for r in ignore_rules(db)][:LIST_CAP]
    # 只要 about 原文：_profile_context 带「【用户画像】…你没有写入画像的能力」这类前缀，会误导分类
    about = (m2.profile_about(db) or "")[:ABOUT_HINT_MAX]
    return (f"当前关注：{'、'.join(focus) or '无'}\n"
            f"当前不看：{'；'.join(rules) or '无'}\n"
            f"关于我（摘要）：{about or '无'}\n\n"
            f"用户这一句：{text.strip()[:USER_TEXT_MAX]}")


def _dedupe(items: list[Intent]) -> list[Intent]:
    """同 kind + topic(casefold) 只留一条，high 优先，保留首次出现的顺序。"""
    best: dict[tuple[str, str], Intent] = {}
    order: list[tuple[str, str]] = []
    for it in items:
        key = (it.kind, it.topic.casefold())
        if key not in best:
            best[key] = it
            order.append(key)
        elif it.high and not best[key].high:
            best[key] = it
    return [best[k] for k in order]


def parse(raw: str | None, user_text: str) -> list[Intent]:
    """严格解析；不合格抛 SchemaError。evidence 不是原话子串 → 降为 low（核对证据，不猜意图）。"""
    if not raw or not raw.strip():
        raise SchemaError("空输出")
    obj = _load_json(raw)
    if not isinstance(obj, dict) or set(obj) != {"intents"}:
        raise SchemaError("顶层必须且只能有 intents")
    items = obj["intents"]
    if not isinstance(items, list) or not items:
        raise SchemaError("intents 为空")
    said = _squash(user_text)
    out: list[Intent] = []
    for it in items[:MAX_INTENTS]:
        if not isinstance(it, dict) or not set(it) <= ITEM_KEYS:
            raise SchemaError("条目字段越界")
        kind, conf = it.get("kind"), it.get("confidence")
        if kind not in KINDS or conf not in CONFIDENCE:
            raise SchemaError(f"非法 kind/confidence：{kind}/{conf}")
        evidence = str(it.get("evidence") or "").strip()[:EVIDENCE_MAX]
        if kind in NEEDS_TOPIC:
            topic = focus_topics.normalize(it.get("topic"))
            if not topic:  # 想改关注/不看却没说改哪条 → 没拿准
                out.append(Intent("unclear", "", evidence, "low"))
                continue
        else:
            topic = str(it.get("topic") or "").strip()[:FREE_TOPIC_MAX]
        if kind not in ("none", "unclear") and (not evidence or _squash(evidence) not in said):
            conf = "low"
        out.append(Intent(kind, topic, evidence, conf))
    acts = _dedupe([i for i in out if i.kind != "none"])  # none 与其他意图混出 → 以其他为准
    return acts or [Intent("none", "", "", "high")]


async def classify(db: Any, ctx: Any, text: str) -> IntentResult:
    if not chat_llm.chat_configured(db, ctx):
        return IntentResult([], status="unavailable", attempts=0, error="对话模型未配置")
    try:
        user = build_user(db, text)
    except Exception as exc:  # 清单读不出来不影响分类；删除类意图在执行时仍按库里实际清单比对
        log.warning("意图清单拼接异常：%s", type(exc).__name__)
        user = f"当前关注：无\n当前不看：无\n关于我（摘要）：无\n\n用户这一句：{text.strip()[:USER_TEXT_MAX]}"
    err = ""
    for attempt in (1, 2):
        u = user if attempt == 1 else f"{user}\n\n上一次输出不合格（{err}）。只输出符合格式的 JSON。"
        # 第 2 次关掉 json 模式：个别兼容服务不认 response_format 会直接 400
        raw = await chat_llm.json_call(db, ctx, INTENT_SYSTEM, u, purpose=PURPOSE_INTENT,
                                       schema=INTENT_JSON_SCHEMA, json_mode=(attempt == 1))
        try:
            intents = parse(raw, text)
        except SchemaError as e:
            err = str(e) if raw is not None else "调用失败"
            log.info("意图解析失败（第 %d 次）：%s", attempt, err)
            continue
        return IntentResult(intents, status="ok" if attempt == 1 else "retry_ok",
                            attempts=attempt, error=err)
    return unclear_result(text, err, attempts=2)


def parse_profile(raw: str | None) -> dict | None:
    """{"about": str, "evidence": [...]} → dict；about 为 null/空 → None；结构不对抛 SchemaError。
    字段合法性（长度、evidence 是否原话、≥4 字）仍交给 profile_write_from_chat，与旧标记路径一致。"""
    if not raw or not raw.strip():
        raise SchemaError("空输出")
    obj = _load_json(raw)
    if not isinstance(obj, dict) or "about" not in obj:
        raise SchemaError("缺 about")
    about = obj.get("about")
    if about is None or (isinstance(about, str) and not about.strip()):
        return None
    if not isinstance(about, str):
        raise SchemaError("about 不是字符串")
    ev = obj.get("evidence")
    if not isinstance(ev, list):
        raise SchemaError("evidence 不是数组")
    evidence = [x.strip() for x in ev if isinstance(x, str) and x.strip()][:5]
    return {"about": about.strip(), "evidence": evidence}


async def extract_profile(db: Any, ctx: Any, text: str) -> tuple[dict | None, str]:
    """返回 (画像载荷, ok/none/fail)。失败重试一次（第 2 次不带 json 模式）。"""
    try:
        about = (m2.profile_about(db) or "").strip()  # 只要 about 原文，不要提示词前缀
    except Exception as exc:
        log.warning("读取「关于我」异常：%s", type(exc).__name__)
        return None, "fail"  # 拿不到现有版本就不能"合并"，宁可不写
    user = f"现有「关于我」：{about or '（空）'}\n\n用户这一句：{text.strip()[:USER_TEXT_MAX]}"
    err = ""
    for attempt in (1, 2):
        u = user if attempt == 1 else f"{user}\n\n上一次输出不合格（{err}）。只输出符合格式的 JSON。"
        raw = await chat_llm.json_call(db, ctx, PROFILE_SYSTEM, u, purpose=PURPOSE_PROFILE,
                                       schema=PROFILE_JSON_SCHEMA, json_mode=(attempt == 1))
        try:
            spec = parse_profile(raw)
        except SchemaError as e:
            err = str(e) if raw is not None else "调用失败"
            log.info("画像载荷解析失败（第 %d 次）：%s", attempt, err)
            continue
        return (spec, "ok") if spec is not None else (None, "none")
    return None, "fail"
