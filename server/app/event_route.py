"""自我陈述分流的纯判定（对话侧在用； 群消息链路复用粒度/日期核对）。

不碰数据库、不调模型、不读时钟（now 由调用方传入），全部可表格测试。
原则：模型只描述"看到一件事"，建不建由这里决定；任何一道核对不过都往"问"的方向降级。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- 触发表（用户对系统提要求）
RECALL_AFTER = ("上次", "上回", "之前", "以前", "那个事", "这件事", "这事")

ASK_TRIGGERS: tuple[str, ...] = (
    "帮我记", "记一下", "记下来", "记着", "提醒我", "提个醒", "别让我忘",
    "加个待办", "建个待办", "加到待办", "记到待办", "到时候叫我", "记得",
)
# "记得"只认祈使用法；前一个字是这些的是回忆用法（我记得 / 还记得 / 不记得）
_RECALL_PREFIX = "我他她还不没都也就"

# ---------------------------------------------------------------- 粒度词表（程序自己的，不信模型）
GRAIN_RANK = {"none": 0, "month": 1, "week": 2, "day": 3}
WEEK_WORDS = ("下下周", "下周", "这周", "本周", "下礼拜", "这礼拜", "下星期", "这星期", "周内")
MONTH_WORDS = ("下个月", "下月", "这个月", "本月", "月底", "月初", "月中", "年底", "年初",
               "年前", "年后", "今年", "明年", "期中", "期末", "寒假", "暑假", "开学")
# 过几天 / 最近 / 改天 …… 不在任何表里 → none
PAST_WORDS = ("昨天", "前天", "上周", "上礼拜", "上星期", "上个月", "上月", "去年", "刚才", "刚刚")

_C = "0-9一二三四五六七八九十两〇零"
_N = f"[{_C}]{{1,3}}"
_NOT_PLACE = "(?![楼线床室房门位栋层])"  # 3号楼 / 2号线 不是日期
_FULL_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_MD_RE = re.compile(rf"({_N})\s*月\s*({_N})\s*[日号]{_NOT_PLACE}")
_SLASH_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_D_RE = re.compile(rf"(?<![{_C}月/])({_N})\s*[日号]{_NOT_PLACE}")
_WEEKDAY_RE = re.compile(r"(?<!每)(下下|下|这|本)?个?(?:周|星期|礼拜)([一二三四五六日天1-7])")
_MONTH_ONLY_RE = re.compile(rf"{_N}\s*月")
_REL_DAYS = (("大后天", 3), ("后天", 2), ("明天", 1), ("明儿", 1), ("明早", 1), ("明晚", 1),
             ("今天", 0), ("今儿", 0), ("今晚", 0))
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
            "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", _nfkc(s))


def _cn_int(s: str) -> int | None:
    s = _nfkc(s).strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_DIGIT.get(a) if a else 1
        ones = _CN_DIGIT.get(b) if b else 0
        return None if tens is None or ones is None else tens * 10 + ones
    return _CN_DIGIT.get(s) if len(s) == 1 else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------- 核对 1：用户有没有提要求
def is_asked(ask_text: str, user_text: str) -> bool:

    # 助手口吻与回忆：不是用户在要求记
    flat = _squash(ask_text)
    if "帮你记" in flat:
        return False
    for phrase in RECALL_AFTER:
        if f"记得{phrase}" in flat or f"记着{phrase}" in flat:
            return False
    """ask_text 必须是本轮原话子串，且命中触发表。"""
    a, u = _squash(ask_text), _squash(user_text)
    if len(a) < 2:
        return False
    pos = u.find(a)
    if pos < 0:
        return False
    for w in ASK_TRIGGERS:
        if w != "记得":
            if w in a:
                return True
            continue
        start = a.find(w)
        while start >= 0:
            i = pos + start
            if i == 0 or u[i - 1] not in _RECALL_PREFIX:
                return True
            start = a.find(w, start + 1)
    return False


# ---------------------------------------------------------------- 核对 2：粒度由程序复核
def program_grain(time_text: str) -> str:
    t = _nfkc(time_text)
    if not t:
        return "none"
    if (_FULL_RE.search(t) or _MD_RE.search(t) or _SLASH_RE.search(t) or _D_RE.search(t)
            or _WEEKDAY_RE.search(t) or any(w in t for w, _ in _REL_DAYS)):
        return "day"
    if any(w in t for w in WEEK_WORDS):
        return "week"
    if any(w in t for w in MONTH_WORDS) or _MONTH_ONLY_RE.search(t):
        return "month"
    return "none"


def due_matches(time_text: str, due: date, today: date) -> bool:
    """due_at 必须和原话里的日期对得上（防模型算错日子/年份）。"""
    t = _nfkc(time_text)
    if m := _FULL_RE.search(t):
        return (due.year, due.month, due.day) == tuple(int(x) for x in m.groups())
    if m := _MD_RE.search(t):
        return (due.month, due.day) == (_cn_int(m.group(1)), _cn_int(m.group(2)))
    if m := _SLASH_RE.search(t):
        return (due.month, due.day) == (int(m.group(1)), int(m.group(2)))
    for w, n in _REL_DAYS:
        if w in t:
            return due == today + timedelta(days=n)
    if m := _WEEKDAY_RE.search(t):
        if due.weekday() != _WEEKDAY[m.group(2)]:
            return False
        weeks = (_monday(due) - _monday(today)).days // 7
        want = {"这": (0,), "本": (0,), "下": (1,), "下下": (2,), None: (0, 1)}[m.group(1)]
        return weeks in want
    if m := _D_RE.search(t):
        if due.day != _cn_int(m.group(1)):
            return False
        if "下个月" in t or "下月" in t:
            nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            return (due.year, due.month) == (nxt.year, nxt.month)
        return True
    return False


# ---------------------------------------------------------------- 核对 3：due_at 能解析
def parse_due(raw: object, now: datetime) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if len(s) <= 10:  # 只给了日期
        dt = dt.replace(hour=23, minute=59)
    tz = now.tzinfo
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    elif tz is not None:
        dt = dt.astimezone(tz)
    else:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- 去重
def norm_title(s: str) -> str:
    t = _nfkc(s).lower()
    return "".join(ch for ch in t if not (ch.isspace() or unicodedata.category(ch)[0] in "PS"))


def same_item(a: str, b: str) -> bool:
    x, y = norm_title(a), norm_title(b)
    if not x or not y:
        return False
    return x == y or (min(len(x), len(y)) >= 4 and (x in y or y in x))


# ---------------------------------------------------------------- 总判定 + 路由
@dataclass
class EventCheck:
    route: str                      # todo | review | drop
    title: str = ""
    asked: bool = False
    grain: str = "none"
    time_text: str = ""
    due_at: str | None = None       # 只有通过全部核对才非空
    ask_reason: str | None = None   # no_request | no_date | both
    dedupe_key: str = ""
    notes: list[str] = field(default_factory=list)


def route(asked: bool, dated: bool) -> tuple[str, str | None]:
    """纯路由表（2026-11-17 用户决定：对话里说了就记）。

    到日 → 待办（用户既然在系统里说了这件事，就算一种要求）；
    不到日 → 待确认，不建待办。不提供"不设日期也记上"。
    """
    if dated:
        return "todo", None
    return "review", ("no_date" if asked else "both")


def verify_event(spec: dict, user_text: str, now: datetime) -> EventCheck:
    notes: list[str] = []
    title = str(spec.get("title") or "").strip()[:100]
    if not title:
        return EventCheck("drop", notes=["没有标题"])
    whose = str(spec.get("whose") or "me").strip().lower()
    if whose not in ("me", "self", "我"):
        return EventCheck("drop", title=title, notes=["别人的事"])

    raw_ask = str(spec.get("ask_text") or "")
    asked = is_asked(raw_ask, user_text)
    if raw_ask.strip() and not asked:
        notes.append("ask_text 不是原话或没命中触发表")

    tt = str(spec.get("time_text") or "").strip()
    if tt and _squash(tt) not in _squash(user_text):
        notes.append("time_text 不是原话")
        tt = ""
    if tt and any(w in _nfkc(tt) for w in PAST_WORDS):
        return EventCheck("drop", title=title, time_text=tt, notes=notes + ["已经过去"])

    mg = str(spec.get("time_grain") or "none").strip().lower()
    mg = mg if mg in GRAIN_RANK else "none"
    pg = program_grain(tt)
    grain = mg if GRAIN_RANK[mg] <= GRAIN_RANK[pg] else pg  # 取两者里更粗的
    if grain != mg:
        notes.append(f"粒度 {mg}→{grain}")

    due_iso: str | None = None
    if grain == "day":
        today = now.date()
        dt = parse_due(spec.get("due_at"), now)
        if dt is None:
            notes.append("due_at 解析不了")
        elif dt.date() < today:
            notes.append("due_at 早于今天")
        elif dt.date() > today + timedelta(days=366):
            notes.append("due_at 超过一年")
        elif not due_matches(tt, dt.date(), today):
            notes.append("due_at 和原话对不上")
        else:
            due_iso = dt.isoformat(timespec="seconds")
    elif spec.get("due_at"):
        notes.append("粒度不到日，丢弃 due_at")

    r, reason = route(asked, due_iso is not None)
    return EventCheck(r, title=title, asked=asked, grain=grain, time_text=tt, due_at=due_iso,
                      ask_reason=reason, dedupe_key=norm_title(title), notes=notes)


# ---------------------------------------------------------------- 画像时间闸门
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;，,、\n]+")
_ONE_OFF_STRONG_RE = re.compile(
    r"大?后天|明天|今天|今晚|明早|明晚|昨天|前天|下下?个?(?:周|星期|礼拜)|下个?月|上个?月"
    r"|月底|月初|年底|年初|今年|明年|去年|(?:19|20)\d{2}\s*年|\d{4}[-/.]\d{1,2}")
_ONE_OFF_WEAK_RE = re.compile(
    rf"(?<!每)(?:周|星期|礼拜)[一二三四五六日天1-7]|{_N}\s*月"
    rf"|(?<![\d月])\d{{1,2}}\s*[日号]{_NOT_PLACE}|(?<![{_C}])[一二三四五六七八九十]{{1,3}}号{_NOT_PLACE}")


def new_clauses(before: str, after: str) -> list[str]:
    old = {c.strip() for c in _CLAUSE_SPLIT_RE.split(_nfkc(before)) if c.strip()}
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(_nfkc(after)) if c.strip() and c.strip() not in old]


def one_off_hits(before: str, after: str) -> list[str]:
    """只查新增分句。含"每"的分句是周期性说法，只查强一次性词（明天/下个月/2026年…）。"""
    hits: list[str] = []
    for c in new_clauses(before, after):
        m = _ONE_OFF_STRONG_RE.search(c) or (None if "每" in c else _ONE_OFF_WEAK_RE.search(c))
        if m:
            hits.append(m.group(0))
    return hits
