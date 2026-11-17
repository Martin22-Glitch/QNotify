""" 步骤 3：回执 → 用户可读行。只读 Receipt（执行器落库后回读的状态），不读模型文字、不读意图。"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from app.intent_exec import Receipt

log = logging.getLogger(__name__)

DONE_STATES = ("created", "already")
_RANK = {"created": 0, "already": 0, "failed": 1, "pending": 2, "unclear": 3}

# 页面名对齐前端真实界面：关注/不看/画像都在「我的偏好」下，待办是独立页。
FOCUS_PAGE = "「我的偏好 → 我关注的」"
IGNORE_PAGE = "「我的偏好 → 不看的」"
PROFILE_PAGE = "「我的偏好 → 关于我」"
TODO_PAGE = "「待办页」"

# kind -> (已写入, 本来就是, 没成的开头, 手动入口, 待确认)。只说发生了什么，不承诺以后。
_TEXT: dict[str, tuple[str, str, str, str, str]] = {
    "focus_add":     ("「{t}」加进关注了", "「{t}」本来就在关注里", "「{t}」没加进关注",
                      FOCUS_PAGE + "手动加", "「{t}」加关注还没生效，等你在卡片上选"),
    "focus_remove":  ("「{t}」从关注里删掉了", "「{t}」本来就不在关注里", "「{t}」没从关注里删掉",
                      FOCUS_PAGE + "手动删", "「{t}」取消关注还没生效，等你在卡片上选"),
    "ignore_add":    ("「{t}」加进不看规则了", "「{t}」本来就在不看规则里", "「{t}」没加进不看规则",
                      IGNORE_PAGE + "手动加", "「{t}」设为不看还没生效，等你在卡片上选"),
    "ignore_remove": ("「{t}」从不看规则里拿掉了", "「{t}」本来就不在不看规则里", "「{t}」没从不看规则里拿掉",
                      IGNORE_PAGE + "手动停用", "「{t}」取消不看还没生效，等你在卡片上选"),
    "profile":       ("画像更新了", "画像里本来就是这样", "画像没更新",
                      PROFILE_PAGE + "手动改", "画像这条还没生效，等你在卡片上选"),
    "event":         ("「{t}」加进待办了", "「{t}」本来就在待办里", "「{t}」没记进待办",
                      TODO_PAGE + "手动加", "「{t}」这件事还没记，等你在卡片上选"),
    "review":        ("「{t}」放进待确认了", "「{t}」本来就在待确认里", "「{t}」没放进待确认",
                      TODO_PAGE + "手动加", "「{t}」还没放进待确认，等你在卡片上选"),
}
_TEXT["rule_disable"] = _TEXT["ignore_remove"]
_GENERIC = ("「{t}」改好了", "「{t}」本来就是这样", "「{t}」没改成", "对应页面手动改", "「{t}」还没生效，等你在卡片上选")

UNCLEAR_LINE = "这句我没拿准要不要记"
INTENT_DOWN_LINE = ("这句没处理成：{why}。本轮没有修改任何设置，"
                    "要改可以去" + FOCUS_PAGE + "或" + PROFILE_PAGE + "手动改")
CRASH_LINE = "这句处理时系统出错：{why}。可能只改了一部分，可以到" + FOCUS_PAGE + "、" + PROFILE_PAGE + "核对"


def _checked(r: Receipt) -> Receipt:
    if r.state not in _RANK:
        log.warning("回执状态未知 %s/%s：按失败显示", r.kind, r.state)
        return replace(r, state="failed", why=r.why or "结果状态不明")
    if r.state in DONE_STATES and not r.row:
        log.warning("回执 %s「%s」声称 %s 但没有回读行：按失败显示", r.kind, r.target, r.state)
        why = "写完没能回读确认" if r.state == "created" else "没能确认它现在的状态"
        return replace(r, state="failed", why=why)
    return r


def collect(ex: Any, *, pure_none: bool) -> list[Receipt]:
    """回执 + 未带回执的确认卡 → 校验、补齐、排序、去重。"""
    rs = [_checked(r) for r in ex.receipts]
    have = {(r.kind, r.target or "") for r in rs}
    for c in ex.pending:
        key = (c.kind, c.topic or "")
        if key not in have:
            rs.append(Receipt(c.kind, c.topic or "", "unclear" if c.kind == "unclear" else "pending"))
            have.add(key)
    if not pure_none and not rs:
        log.warning("有写入意图但没有任何回执：按没拿准显示")
        rs.append(Receipt("unclear", "", "unclear"))
    out, seen = [], set()
    for r in sorted(rs, key=lambda r: _RANK[r.state]):
        k = (r.kind, r.target, r.state)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def line(r: Receipt) -> str:
    if r.kind == "intent_down":
        return INTENT_DOWN_LINE.format(why=r.why or "系统出错")
    if r.kind == "crash":
        return CRASH_LINE.format(why=r.why or "系统出错")
    if r.state == "unclear":
        return UNCLEAR_LINE
    created, already, failed, manual, pending = _TEXT.get(r.kind, _GENERIC)
    t = (r.target or "").strip() or "这一项"
    if r.state == "created":
        return created.format(t=t)
    if r.state == "already":
        return already.format(t=t)
    if r.state == "pending":
        return pending.format(t=t)
    return f"{failed.format(t=t)}：{r.why or '系统出错'}，可以在{manual}"


def render(rs: list[Receipt]) -> list[str]:
    out: list[str] = []
    for r in rs:
        s = line(r)
        if s not in out:
            out.append(s)
    return out


def fact_note(rs: list[Receipt]) -> str:
    """混合轮次第二次调用的事实块：与用户看到的回执行逐字相同。"""
    done = [line(r) for r in rs if r.state in DONE_STATES]
    undone = [line(r) for r in rs if r.state not in DONE_STATES]
    return ("【本轮事实】本轮已执行：" + ("；".join(done) or "无")
            + "；未执行：" + ("；".join(undone) or "无") + "。\n"
            "【说话规则】上面这些结果会原样显示在你的回复上方，不要复述。只回答用户这句话里的问题；"
            "提到设置时只能依据上面的事实，不要说列表以外的任何改动，不要承诺以后会怎么处理，"
            "不要问用户要不要记，不要告诉用户该怎么说，不要输出任何标记或分隔线。")


def card_reply(ex: Any, fallback: str) -> str:
    """确认卡点击后的回复（resolve 的调用方用）。没有回执时用 fallback（如 DISMISS_LINE）。"""
    return "\n".join(render(collect(ex, pure_none=True))) or fallback
