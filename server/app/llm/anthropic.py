"""Anthropic Messages 协议：POST {base}/messages，用 tool_use 强制结构化输出。

注意：本文件与 anthropic SDK 无关，只用 httpx 发请求（由 transport 负责）。
"""

from __future__ import annotations

import json
from typing import Any

from .config import LLMConfig
from .parse import DECISION_JSON_SCHEMA, DecisionError, loads_json_object_tolerant

ANTHROPIC_VERSION = "2023-06-01"
TOOL_NAME = "emit_decision"
MAX_TOKENS = 1024


def build_request(
    config: LLMConfig, system_prompt: str, user_prompt: str, *, thinking: bool = True
) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = f"{config.resolved_base_url()}/messages"
    headers = {
        "x-api-key": (config.api_key or "").strip(),
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": (config.model or "").strip(),
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": [
            {
                "name": TOOL_NAME,
                "description": "输出判定结果",
                "input_schema": DECISION_JSON_SCHEMA,
            }
        ],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
        "temperature": 0,
    }
    # 只对认得的厂商发；Anthropic 官方与 DeepSeek 的写法不同，认不出就不发
    from app.llm import thinking as thinking_dialect

    thinking_dialect.apply_anthropic(body, thinking=thinking, base_url=config.resolved_base_url())
    return url, headers, body


def extract(data: Any) -> tuple[str, Any]:
    """返回 (原始文本, 解析出的对象)；失败抛 DecisionError（带 raw）。"""
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise DecisionError("响应缺少 content 数组", raw=_dump(data))

    tool_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    block = next((b for b in tool_blocks if b.get("name") == TOOL_NAME), None)
    if block is None and tool_blocks:
        block = tool_blocks[0]
    if block is None:
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        raw = "".join(t for t in texts if isinstance(t, str)) or _dump(data)
        raise DecisionError("响应里没有 tool_use", raw=raw)

    payload = block.get("input")
    if isinstance(payload, str):  # 个别代理会把 input 序列化成字符串
        try:
            payload = loads_json_object_tolerant(payload)
        except DecisionError as exc:
            raise DecisionError(str(exc), raw=block.get("input")) from exc
    return _dump(payload), payload


def _dump(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)
