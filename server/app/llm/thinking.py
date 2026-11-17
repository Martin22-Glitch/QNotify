"""思考模式的"方言"识别。

为什么需要它：这个产品是**模型无关**的——用户可能把 base_url 换成 OpenAI、Anthropic、
通义、智谱、本地 Ollama……而"关思考"这件事各家写法完全不同，甚至有的厂商没有这个概念：

    DeepSeek(OpenAI 协议)   {"thinking": {"type": "disabled"}}
    DeepSeek(Anthropic 协议) {"reasoning": {"effort": "none"}}
    Anthropic 官方(Claude)   {"thinking": {"type": "disabled"}} 是另一种结构，传错会 400
    其它兼容厂商             不认识这些字段，传了可能直接 400

之前把这些字段无条件塞进请求体，等于**只对 DeepSeek 成立**，换成别家就会报错。
所以现在：只对认得的厂商发，其余一律不发（保持请求体干净）。

判定方式保守：看 base_url 的主机名里有没有 deepseek。认不出就当"不支持"，不发任何额外字段。
"""

from __future__ import annotations

from typing import Any

STYLE_DEEPSEEK = "deepseek"
STYLE_NONE = "none"

DEEPSEEK_HOST = "deepseek.com"


def style_for(base_url: str | None) -> str:
    host = (base_url or "").strip().lower()
    if DEEPSEEK_HOST in host:
        return STYLE_DEEPSEEK
    return STYLE_NONE


def supports_thinking_switch(base_url: str | None) -> bool:
    return style_for(base_url) != STYLE_NONE


def apply_openai(body: dict[str, Any], *, thinking: bool, base_url: str | None) -> dict[str, Any]:
    """OpenAI 协议：只有认得的厂商才动请求体；thinking=True 时什么都不加。"""
    if thinking or style_for(base_url) != STYLE_DEEPSEEK:
        return body
    body["thinking"] = {"type": "disabled"}
    return body


def apply_anthropic(body: dict[str, Any], *, thinking: bool, base_url: str | None) -> dict[str, Any]:
    if thinking or style_for(base_url) != STYLE_DEEPSEEK:
        return body
    body["reasoning"] = {"effort": "none"}
    return body


def describe(detail: bool = False) -> dict[str, Any]:
    """给前端/设置页看的说明：这个开关在什么情况下有效。"""
    data: dict[str, Any] = {
        "applies_to": "仅 DeepSeek（base_url 含 deepseek.com）",
        "default": "开启思考",
        "note": "其它厂商不发任何额外字段，避免请求被拒",
    }
    if detail:
        data["styles"] = {STYLE_DEEPSEEK: "thinking.type=disabled / reasoning.effort=none", STYLE_NONE: "不发送"}
    return data
