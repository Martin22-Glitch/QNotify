"""OneBot v11 适配器：把正向 WebSocket 客户端包装成 Adapter。

两种构造方式：
- from_config(cfg)：自己持有一个客户端（main.py 迁移后使用）
- attach_existing(ctx)：包装应用上下文里已经在跑的客户端（当前的桥接方式，不另起连接）
全程只读：只用 get_* 类查询动作。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import (
    CAP_FRIENDS,
    CAP_GROUPS,
    CAP_HISTORY,
    CAP_MEMBERS,
    CAP_RESOURCES,
    Capabilities,
    EventHandler,
    NotSupported,
)
from app.adapters.config import AdapterConfig
from app.models import IncomingMessage
from app.napcat import events
from app.napcat.client import NapCatClient

log = logging.getLogger(__name__)

RESOURCE_MAX_BYTES = 64 * 1024 * 1024


def _ctx_values(ctx: Any) -> list[Any]:
    if ctx is None:
        return []
    values = list((getattr(ctx, "__dict__", None) or {}).values())
    for cls in type(ctx).__mro__:
        slots = getattr(cls, "__slots__", ()) or ()
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            try:
                values.append(getattr(ctx, slot))
            except AttributeError:
                continue
    return values


def find_client(ctx: Any) -> NapCatClient | None:
    """在应用上下文里找到正在使用的 OneBot WS 客户端（按类型找，不依赖字段名）。"""
    for value in _ctx_values(ctx):
        if isinstance(value, NapCatClient):
            return value
    return None


def _as_dict_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


class OneBot11Adapter:
    name = "onebot11"
    DEFAULT_CAPABILITIES = Capabilities({CAP_GROUPS, CAP_MEMBERS, CAP_FRIENDS, CAP_HISTORY, CAP_RESOURCES})

    def __init__(
        self,
        *,
        url: str = "",
        token: str = "",
        client: NapCatClient | None = None,
        capabilities: Any = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client if client is not None else NapCatClient(url, token)
        self.capabilities = Capabilities(self.DEFAULT_CAPABILITIES if capabilities is None else capabilities)
        self._handlers: list[EventHandler] = []

    # ---------- 构造 ----------
    @classmethod
    def from_config(cls, cfg: AdapterConfig) -> OneBot11Adapter:
        return cls(url=cfg.ws_url, token=cfg.token)

    @classmethod
    def attach_existing(cls, ctx: Any, previous: Any = None) -> OneBot11Adapter | None:
        client = find_client(ctx)
        if client is None:
            return None
        if isinstance(previous, cls) and previous.client is client:
            return previous
        return cls(client=client)

    @classmethod
    def reconfigure_existing(cls, ctx: Any, cfg: AdapterConfig) -> bool:
        client = find_client(ctx)
        if client is None:
            return False
        client.reconfigure(cfg.ws_url, cfg.token)
        return True

    @property
    def client(self) -> NapCatClient:
        return self._client

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self._client.start()  # 客户端自身幂等，内部负责重连

    async def stop(self) -> None:
        if self._owns_client:
            await self._client.stop()

    @property
    def connected(self) -> bool:
        return bool(self._client.connected)

    async def login_status(self) -> dict[str, Any]:
        c = self._client
        logged_in = bool(c.logged_in)
        return {
            "logged_in": logged_in,
            "self_id": c.self_id if logged_in else None,
            "nickname": c.nickname if logged_in else None,
        }

    # ---------- 事件 ----------
    def on_event(self, handler: EventHandler) -> None:
        if not self._handlers:
            self._client.add_listener(self._dispatch)
        self._handlers.append(handler)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        msg = events.parse(frame)
        if msg is None:
            return
        for handler in list(self._handlers):
            try:
                await handler(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("适配器事件处理失败：%s", type(exc).__name__)

    # ---------- 查询 ----------
    def _require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise NotSupported(capability)

    async def list_groups(self) -> list[dict[str, Any]]:
        self._require(CAP_GROUPS)
        return _as_dict_list(await self._client.call("get_group_list"))

    async def list_members(self, group_id: int) -> list[dict[str, Any]]:
        self._require(CAP_MEMBERS)
        return _as_dict_list(await self._client.call("get_group_member_list", {"group_id": int(group_id)}))

    async def list_friends(self) -> list[dict[str, Any]]:
        self._require(CAP_FRIENDS)
        return _as_dict_list(await self._client.call("get_friend_list"))

    async def fetch_history(self, group_id: int, count: int) -> list[IncomingMessage]:
        self._require(CAP_HISTORY)
        data = await self._client.call("get_group_msg_history", {"group_id": int(group_id), "count": int(count)})
        items = data.get("messages") if isinstance(data, dict) else data
        out: list[IncomingMessage] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                msg = events.parse_history(item, int(group_id))
                if msg is not None:
                    out.append(msg)
        return out

    async def fetch_resource(self, ref: str) -> bytes:
        self._require(CAP_RESOURCES)
        data = await self._client.call("get_image", {"file": ref})
        if not isinstance(data, dict):
            data = await self._client.call("get_file", {"file_id": ref})
        if not isinstance(data, dict):
            raise FileNotFoundError("协议端没有返回该资源")
        b64 = data.get("base64")
        if isinstance(b64, str) and b64:
            return base64.b64decode(b64)
        path = data.get("file")
        if isinstance(path, str) and path:
            p = Path(path)
            if await asyncio.to_thread(p.is_file):
                if await asyncio.to_thread(lambda: p.stat().st_size) > RESOURCE_MAX_BYTES:
                    raise ValueError("资源过大")
                return await asyncio.to_thread(p.read_bytes)
        url = data.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.get(url)
                resp.raise_for_status()
                if len(resp.content) > RESOURCE_MAX_BYTES:
                    raise ValueError("资源过大")
                return resp.content
        raise FileNotFoundError("协议端返回的资源信息不可用")
