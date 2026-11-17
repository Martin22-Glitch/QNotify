"""结构化解析与本地校验。校验不过一律抛 DecisionError，由调用方映射为 llm_parse_error。

新增可选事实字段 kind / items[].audience_type / audience_text（以及事项级 kind、
optional_hint）。这些字段采用宽松校验：取值非法视同缺失、不抛错，保证新字段写坏时
整条判定仍可用，并由 m2 回退到旧阈值逻辑。字段缺失时输出与旧版逐键一致。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

MAX_ITEMS = 20

KINDS = ("directive", "optional", "info", "chat", "noise")
AUDIENCE_TYPES = ("me", "all", "conditional", "role_me", "role", "other", "unknown")
AUDIENCE_TEXT_MAX = 60

DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean", "description": "用户本人是否需要处理"},
        "kind": {
            "type": "string",
            "enum": list(KINDS),
            "description": "消息性质：directive 明确要求 / optional 自愿参与 / info 通知宣传 / chat 闲聊 / noise 噪声",
        },
        "items": {
            "type": "array",
            "description": "需要做的事项；actionable=false 时为空数组",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "简短动宾短语"},
                    "detail": {"type": "string", "description": "补充信息，没有填空字符串"},
                    "due_at": {
                        "type": ["string", "null"],
                        "description": "带时区偏移的 ISO 8601；消息没说清就填 null，禁止编造",
                    },
                    "audience": {"type": "string", "description": "适用对象，不明确填空字符串"},
                    "audience_type": {
                        "type": "string",
                        "enum": list(AUDIENCE_TYPES),
                        "description": "这件事针对谁：me/all/conditional/role_me/role/other/unknown",
                    },
                    "audience_text": {"type": "string", "description": "条件或身份的原话，没有填空字符串"},
                    "kind": {
                        "type": "string",
                        "enum": list(KINDS),
                        "description": "可选：该事项的性质与整条消息不同时填写",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["title", "detail", "due_at", "audience", "confidence"],
            },
        },
        "reason": {"type": "string", "description": "一句话判断理由"},
        "evidence": {"type": "string", "description": "逐字摘抄的原文依据"},
    },
    "required": ["actionable", "items", "reason", "evidence"],
}


class DecisionError(ValueError):
    """模型返回无法变成合法判定。raw 为可保留的原文（可为空）。"""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")
_WS_RE = re.compile(r"\s+")
_QUOTES = "\"'“”‘’「」『』"


def loads_json_object(text: str) -> dict[str, Any]:
    """把模型文本解析成 JSON 对象；容忍 ```json 围栏和前后少量废话。"""
    if not isinstance(text, str) or not text.strip():
        raise DecisionError("返回为空", raw=text if isinstance(text, str) else "")
    s = _FENCE_RE.sub("", text.strip()).strip()
    candidates = [s]
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        candidates.append(s[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    raise DecisionError("返回不是合法 JSON 对象", raw=text)


def _req_str(value: Any, name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise DecisionError(f"{name} 必须是字符串")
    v = value.strip()
    if not allow_empty and not v:
        raise DecisionError(f"{name} 不能为空")
    return v


def _opt_str(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DecisionError(f"{name} 必须是字符串或 null")
    return value.strip()


def _opt_enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    """宽松枚举：不是字符串或不在取值范围内 → None（视同缺失，不抛错）。"""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in allowed else None


def _soft_str(value: Any, limit: int) -> str:
    """宽松字符串：不是字符串 → 空串；截断到 limit。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _validate_due(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecisionError("due_at 必须是字符串或 null")
    s = value.strip()
    if not s or s.lower() in ("null", "none"):
        return None
    probe = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        datetime.fromisoformat(probe)
    except ValueError as exc:
        raise DecisionError("due_at 不是合法的 ISO 8601 时间") from exc
    return s


def _validate_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError("confidence 必须是数字")
    f = float(value)
    if not math.isfinite(f) or f < 0.0 or f > 1.0:
        raise DecisionError("confidence 必须在 0~1 之间")
    return f


def _validate_item(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise DecisionError(f"items[{index}] 必须是对象")
    out: dict[str, Any] = {
        "title": _req_str(item.get("title"), f"items[{index}].title", allow_empty=False),
        "detail": _opt_str(item.get("detail"), f"items[{index}].detail"),
        "due_at": _validate_due(item.get("due_at")),
        "audience": _opt_str(item.get("audience"), f"items[{index}].audience"),
        "confidence": _validate_confidence(item.get("confidence")),
    }
    #  事实字段：只在合法且非空时带上，旧格式输出与之前逐键一致
    audience_type = _opt_enum(item.get("audience_type"), AUDIENCE_TYPES)
    if audience_type:
        out["audience_type"] = audience_type
    audience_text = _soft_str(item.get("audience_text"), AUDIENCE_TEXT_MAX)
    if audience_text:
        out["audience_text"] = audience_text
    kind = _opt_enum(item.get("kind"), KINDS)
    if kind:
        out["kind"] = kind
    hint = _soft_str(item.get("optional_hint"), AUDIENCE_TEXT_MAX)
    if hint:
        out["optional_hint"] = hint
    return out


def _squash(s: str) -> str:
    return _WS_RE.sub("", s)


def _check_evidence(evidence: str, source_text: str) -> str:
    """evidence 必须出自原文；对不上就清空（不让编造的依据展示给用户）。"""
    ev = evidence.strip()
    if not ev or not source_text:
        return ev
    for cand in (ev, ev.strip(_QUOTES).strip()):
        if cand and (cand in source_text or _squash(cand) in _squash(source_text)):
            return cand
    return ""


def validate_decision(obj: Any, source_text: str = "") -> dict[str, Any]:
    """校验并规范化判定结构；不合法抛 DecisionError。"""
    if not isinstance(obj, dict):
        raise DecisionError("判定结果不是对象")
    actionable = obj.get("actionable")
    if not isinstance(actionable, bool):
        raise DecisionError("actionable 必须是布尔值")
    items_raw = obj.get("items")
    if not isinstance(items_raw, list):
        raise DecisionError("items 必须是数组")
    if "reason" not in obj or "evidence" not in obj:
        raise DecisionError("缺少 reason 或 evidence")
    reason = _req_str(obj.get("reason"), "reason")
    evidence = _req_str(obj.get("evidence"), "evidence")
    if not actionable and items_raw:
        raise DecisionError("actionable=false 时 items 必须为空")
    if actionable and not items_raw:
        raise DecisionError("actionable=true 时至少需要一个事项")
    if len(items_raw) > MAX_ITEMS:
        raise DecisionError("事项数量异常")
    items = [_validate_item(it, i) for i, it in enumerate(items_raw)]
    out: dict[str, Any] = {
        "actionable": actionable,
        "items": items,
        "reason": reason,
        "evidence": _check_evidence(evidence, source_text),
    }
    kind = _opt_enum(obj.get("kind"), KINDS)
    if kind:
        out["kind"] = kind
    return out


_QUOTE_OK = set(',}]:')
_CLOSERS = set(',}]:')


def _repair_string_quotes(s: str) -> str:
    """修常见的"字符串里夹了未转义双引号"。

    模型经常把中文引号写成 ASCII 的 "（例如：用"任何形式都可参赛"等措辞），
    直接 json.loads 会失败。这里按状态机判断：字符串内部的 " 若后面紧跟
    结构字符（, } ] :）才当作结束引号，否则转义。
    """
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j >= n or s[j] in _CLOSERS:
                    out.append(ch)
                    in_str = False
                else:
                    out.append('\\"')
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
        i += 1
    return "".join(out)


def loads_json_object_tolerant(text: str) -> dict[str, Any]:
    """先按标准解析；失败后修一次"字符串内未转义引号"再解析。"""
    try:
        return loads_json_object(text)
    except DecisionError:
        pass
    s = _FENCE_RE.sub("", (text or "").strip()).strip()
    start, end = s.find("{"), s.rfind("}")
    body = s[start : end + 1] if 0 <= start < end else s
    try:
        obj = json.loads(_repair_string_quotes(body))
    except (json.JSONDecodeError, ValueError) as exc:
        raise DecisionError("返回不是合法 JSON 对象", raw=text) from exc
    if not isinstance(obj, dict):
        raise DecisionError("返回不是 JSON 对象", raw=text)
    return obj
