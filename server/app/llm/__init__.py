"""QQ 通知管理器的判定模块。对外：LLMConfig / JudgeResult / judge。

judge 已包含"思考模型不支持强制 tool_choice"的分类与一次降级重试。
"""

from __future__ import annotations

from typing import Any

from app.llm.config import LLMConfig
from app.llm.result import JudgeResult
from app.llm.upstream import ERR_UNSUPPORTED_TOOL_CHOICE


async def judge(**kwargs: Any) -> JudgeResult:
    from app.llm.upstream import judge_with_fallback

    return await judge_with_fallback(**kwargs)


__all__ = ["ERR_UNSUPPORTED_TOOL_CHOICE", "JudgeResult", "LLMConfig", "judge"]
