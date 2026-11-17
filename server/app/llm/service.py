"""judge() 主流程：配置检查 → 拼提示词 → 按 provider 发请求 → 解析 → 本地校验。

任何失败都用 JudgeResult.error 表达，绝不向上抛异常。
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

import httpx

from . import anthropic as anthropic_protocol
from . import openai as openai_protocol
from .config import PROVIDER_ANTHROPIC, PROVIDER_OPENAI, LLMConfig
from .parse import DecisionError, validate_decision
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .result import ERR_LLM, ERR_NOT_CONFIGURED, ERR_PARSE, JudgeResult, truncate_raw
from .transport import post_with_retry

logger = logging.getLogger("app.llm")

_PROTOCOLS: dict[str, ModuleType] = {
    PROVIDER_OPENAI: openai_protocol,
    PROVIDER_ANTHROPIC: anthropic_protocol,
}


async def judge(
    *,
    config: LLMConfig,
    text: str,
    sender_name: str = "",
    group_name: str = "",
    profile: dict[str, str] | None = None,
    focus: str = "",
    samples: list[dict] | None = None,
    today: str = "",
    client: httpx.AsyncClient | None = None,
    timeout: float = 30.0,
    thinking: bool = True,
) -> JudgeResult:
    provider = getattr(config, "normalized_provider", "") or ""
    model = getattr(config, "model", "") or ""
    try:
        return await _judge(
            config=config,
            provider=provider,
            model=model,
            text=text or "",
            sender_name=sender_name or "",
            group_name=group_name or "",
            profile=profile,
            focus=focus,
            samples=samples,
            today=today or "",
            client=client,
            timeout=timeout,
            thinking=thinking,
        )
    except Exception as exc:  # noqa: BLE001  兜底：判定失败不能拖垮调用方
        logger.error("LLM 判定发生意外错误：%s", type(exc).__name__)
        return JudgeResult(ok=False, provider=provider, model=model, error=ERR_LLM)


async def _judge(
    *,
    config: LLMConfig,
    provider: str,
    model: str,
    text: str,
    sender_name: str,
    group_name: str,
    profile: dict[str, Any] | None,
    focus: str = "",
    samples: list[dict[str, Any]] | None = None,
    today: str,
    client: httpx.AsyncClient | None,
    timeout: float,
    thinking: bool = True,
) -> JudgeResult:
    def fail(error: str, raw: str = "") -> JudgeResult:
        return JudgeResult(
            ok=False, decision=None, provider=provider, model=model, raw=truncate_raw(raw), error=error
        )

    protocol = _PROTOCOLS.get(provider)
    if not config.configured or protocol is None:
        return fail(ERR_NOT_CONFIGURED)

    user_prompt = build_user_prompt(
        text=text,
        sender_name=sender_name,
        group_name=group_name,
        profile=profile,
        focus=focus,
        samples=samples,
        today=today,
    )
    url, headers, body = protocol.build_request(config, SYSTEM_PROMPT, user_prompt, thinking=thinking)

    if client is None:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as own_client:
            outcome = await post_with_retry(own_client, url, headers, body, timeout)
    else:
        outcome = await post_with_retry(client, url, headers, body, timeout)

    if outcome.error is not None:
        return fail(outcome.error)
    if outcome.data is None:
        return fail(ERR_PARSE, raw=outcome.text)

    try:
        raw, obj = protocol.extract(outcome.data)
    except DecisionError as exc:
        return fail(ERR_PARSE, raw=exc.raw or outcome.text)

    try:
        decision = validate_decision(obj, text)
    except DecisionError as exc:
        logger.info("LLM 判定结构校验未通过：%s", exc)
        res = fail(ERR_PARSE, raw=raw)
        return JudgeResult(**{**res.__dict__, "usage": _usage_of(outcome.data), "thinking": thinking})

    return JudgeResult(
        ok=True, decision=decision, provider=provider, model=model, raw=truncate_raw(raw), error=None,
        usage=_usage_of(outcome.data), thinking=thinking,
    )
def _usage_of(data: Any) -> dict[str, Any] | None:
    """从响应体里取 usage（OpenAI / Anthropic 两种结构都认）。"""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else None
