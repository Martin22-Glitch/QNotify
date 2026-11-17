"""HTTP 发送与重试。只负责把错误分类，不解析业务结构。

分类规则：
- 401/403                         → llm_auth_error，不重试
- 超时 / 连接失败 / 5xx / 429      → llm_error，重试 1 次（退避 1s）
- 其它 4xx / 无效地址等            → llm_error，不重试
- 2xx 但响应体不是 JSON            → data=None，交由上层判 llm_parse_error
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .result import ERR_AUTH, ERR_LLM

logger = logging.getLogger("app.llm")

MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True)
class HttpOutcome:
    error: str | None
    data: Any = None
    text: str = ""
    status: int | None = None


async def _post_once(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[HttpOutcome, bool]:
    """返回 (结果, 是否值得重试)。日志里只记异常类型与状态码，不记请求头。"""
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TransportError as exc:  # 包含超时、连接失败
        logger.warning("LLM 请求网络错误：%s", type(exc).__name__)
        return HttpOutcome(ERR_LLM), True
    except httpx.HTTPError as exc:
        logger.warning("LLM 请求失败：%s", type(exc).__name__)
        return HttpOutcome(ERR_LLM), False
    except Exception as exc:  # noqa: BLE001  无效 URL 等，不能抛给调用方
        logger.warning("LLM 请求无法发出：%s", type(exc).__name__)
        return HttpOutcome(ERR_LLM), False

    status = resp.status_code
    if status in (401, 403):
        logger.warning("LLM 鉴权失败：HTTP %s", status)
        return HttpOutcome(ERR_AUTH, status=status), False
    if status >= 500 or status == 429:
        logger.warning("LLM 服务端错误：HTTP %s", status)
        return HttpOutcome(ERR_LLM, status=status), True
    if status >= 400:
        logger.warning("LLM 请求被拒绝：HTTP %s", status)
        return HttpOutcome(ERR_LLM, status=status), False

    text = resp.text
    try:
        data = resp.json()
    except ValueError:
        return HttpOutcome(None, data=None, text=text, status=status), False
    return HttpOutcome(None, data=data, text=text, status=status), False


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> HttpOutcome:
    outcome = HttpOutcome(ERR_LLM)
    for attempt in range(MAX_ATTEMPTS):
        outcome, retryable = await _post_once(client, url, headers, body, timeout)
        if outcome.error is None or not retryable or attempt == MAX_ATTEMPTS - 1:
            return outcome
        await asyncio.sleep(RETRY_BACKOFF_SECONDS)
    return outcome
