"""判定结果与错误码。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ERR_NOT_CONFIGURED = "not_configured"
ERR_AUTH = "llm_auth_error"
ERR_LLM = "llm_error"
ERR_PARSE = "llm_parse_error"

RAW_LIMIT = 8000


def truncate_raw(raw: str | None) -> str:
    if not raw:
        return ""
    return raw[:RAW_LIMIT]


@dataclass(frozen=True)
class JudgeResult:
    ok: bool
    decision: dict[str, Any] | None = None  # 成功时为判定结构
    provider: str = ""
    model: str = ""
    raw: str = ""  # 模型原始返回，截断到 8000 字符
    error: str | None = None  # not_configured | llm_auth_error | llm_error | llm_parse_error
    usage: dict[str, Any] | None = None  # 服务端返回的 usage，用于记账
    thinking: bool = True  # 本次调用是否开了思考模式
