"""OpenAI 兼容协议：POST {base}/chat/completions，response_format=json_object。

注意：本文件与 openai SDK 无关，只用 httpx 发请求（由 transport 负责）。
"""

from __future__ import annotations

import json
from typing import Any

from .config import LLMConfig
from .parse import DecisionError, loads_json_object_tolerant


def build_request(
    config: LLMConfig, system_prompt: str, user_prompt: str, *, thinking: bool = True
) -> tuple[str, dict[str, str], dict[str, Any]]:
    url = f"{config.resolved_base_url()}/chat/completions"
    headers = {
        "Authorization": f"Bearer {(config.api_key or '').strip()}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": (config.model or "").strip(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    # 只对认得的厂商发"关思考"字段；别家不发，避免请求被拒（见 app/llm/thinking.py）
    from app.llm import thinking as thinking_dialect

    thinking_dialect.apply_openai(body, thinking=thinking, base_url=config.resolved_base_url())
    return url, headers, body


def _content_to_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 个别兼容厂商返回分段内容
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
        return "".join(parts) if parts else None
    return None


def extract(data: Any) -> tuple[str, Any]:
    """返回 (原始文本, 解析出的对象)；失败抛 DecisionError（带 raw）。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DecisionError("响应缺少 choices[0].message.content", raw=_dump(data)) from exc
    text = _content_to_text(content)
    if text is None or not text.strip():
        raise DecisionError("模型返回内容为空", raw=_dump(data))
    try:
        obj = loads_json_object_tolerant(text)
    except DecisionError as exc:
        raise DecisionError(str(exc), raw=text) from exc
    return text, obj


def _dump(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)
