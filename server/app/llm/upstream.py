"""上游错误分类与思考模型降级（第 8 节）。

不改 transport/service：通过 httpx 事件钩子观察每次响应，
- 识别"不支持强制 tool_choice"的 400/422；
- 记录上游 error.message 原文（供"测试连接"透出）。
降级重试由 judge_with_fallback 完成：第一次结果失败且命中该分类 → 去掉 tool_choice 再调一次。
日志只记状态码与分类，不记请求体、不记密钥。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

log = logging.getLogger(__name__)

ERR_UNSUPPORTED_TOOL_CHOICE = "llm_unsupported_tool_choice"
FORCE_TOOL_HINT = "\n\n必须调用 emit_decision 工具输出结果，不要输出其它文字。"
MESSAGE_LIMIT = 500


@dataclass
class Observation:
    tool_choice_rejected: bool = False
    status: int | None = None
    message: str = ""
    drop_tool_choice: bool = False
    statuses: list[int] = field(default_factory=list)


_OBS: contextvars.ContextVar[Observation | None] = contextvars.ContextVar("qn_llm_obs", default=None)


def upstream_message(text: str) -> str:
    try:
        data = json.loads(text or "")
    except ValueError:
        return (text or "").strip()[:MESSAGE_LIMIT]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:MESSAGE_LIMIT]
    if isinstance(err, str):
        return err[:MESSAGE_LIMIT]
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])[:MESSAGE_LIMIT]
    return (text or "").strip()[:MESSAGE_LIMIT]


def is_tool_choice_rejection(status: int, text: str) -> bool:
    low = (text or "").lower()
    if "tool_choice" not in low:
        return False
    if status in (400, 422) and ("thinking" in low or "not support" in low):
        return True
    return status == 400 and "invalid_request_error" in low


async def _on_request(request: httpx.Request) -> None:
    obs = _OBS.get()
    if obs is None or not obs.drop_tool_choice:
        return
    try:
        body = json.loads(request.content or b"{}")
    except ValueError:
        return
    if not isinstance(body, dict) or "tool_choice" not in body:
        return
    body.pop("tool_choice", None)
    if isinstance(body.get("system"), str):
        body["system"] += FORCE_TOOL_HINT
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request.stream = httpx.ByteStream(data)
    request._content = data
    request.headers["Content-Length"] = str(len(data))


async def _on_response(response: httpx.Response) -> None:
    obs = _OBS.get()
    if obs is None:
        return
    obs.statuses.append(response.status_code)
    if response.status_code < 400:
        return
    await response.aread()
    text = response.text
    obs.status = response.status_code
    obs.message = upstream_message(text)
    if is_tool_choice_rejection(response.status_code, text):
        obs.tool_choice_rejected = True
    log.warning("模型上游返回 %s（tool_choice 受限=%s）", response.status_code, obs.tool_choice_rejected)


def _trust_env() -> bool:
    return os.environ.get("QN_LLM_TRUST_ENV", "0").strip().lower() in ("1", "true", "yes")


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, trust_env=_trust_env(),
                             event_hooks={"request": [_on_request], "response": [_on_response]})


async def judge_with_fallback(*, surface_message: bool = False, **kwargs: Any) -> Any:
    """调用 service.judge；遇到 tool_choice 限制自动去掉它重试一次。"""
    from app.llm.service import judge as base_judge

    if kwargs.get("client") is not None:  # 调用方自带客户端：不介入
        return await base_judge(**kwargs)
    timeout = float(kwargs.get("timeout", 30.0))
    obs = Observation()
    token = _OBS.set(obs)
    try:
        async with _client(timeout) as client:
            res = await base_judge(**{**kwargs, "client": client})
            if not res.ok and obs.tool_choice_rejected:
                obs.tool_choice_rejected = False
                obs.drop_tool_choice = True
                log.warning("上游不支持强制 tool_choice，去掉后重试一次")
                res = await base_judge(**{**kwargs, "client": client})
                if not res.ok and res.error not in ("llm_parse_error",):
                    res = replace(res, error=ERR_UNSUPPORTED_TOOL_CHOICE)
            elif not res.ok and obs.message and res.error == "llm_error" and obs.status == 400:
                pass
    finally:
        _OBS.reset(token)
    if surface_message and not res.ok and obs.message:
        res = replace(res, error=f"{res.error}: {obs.message}")
    return res
