"""适配器接口与能力集合（第 1、4 节）。

一个适配器实例 = 一个协议端连接 = 一个账号。事件一律翻译成 IncomingMessage，
上层（闸门、管线、待办、对话、记忆）不感知原始协议。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.models import IncomingMessage

CAP_GROUPS = "groups"
CAP_MEMBERS = "members"
CAP_FRIENDS = "friends"
CAP_HISTORY = "history"
CAP_RESOURCES = "resources"

# 规范顺序：接口输出按这个顺序排列，便于前端与比较
KNOWN_CAPABILITIES: tuple[str, ...] = (CAP_GROUPS, CAP_MEMBERS, CAP_FRIENDS, CAP_HISTORY, CAP_RESOURCES)

# 能力缺失时给用户看的原因（数据驱动：只看能力，不看适配器名）
UNSUPPORTED_DETAIL: dict[str, str] = {
    CAP_GROUPS: "当前协议端不支持列出群",
    CAP_MEMBERS: "当前协议端不支持群成员列表",
    CAP_FRIENDS: "当前协议端不支持好友列表",
    CAP_HISTORY: "当前协议端不支持历史消息",
    CAP_RESOURCES: "当前协议端不支持获取附件",
}


class Capabilities(frozenset):
    """不可变的能力集合。"""

    def ordered(self) -> list[str]:
        known = [c for c in KNOWN_CAPABILITIES if c in self]
        return known + sorted(str(c) for c in self if c not in KNOWN_CAPABILITIES)


class NotSupported(Exception):
    """协议端没有声明某项能力时抛出。"""

    def __init__(self, capability: str, detail: str = "") -> None:
        self.capability = capability
        super().__init__(detail or UNSUPPORTED_DETAIL.get(capability, f"当前协议端不支持：{capability}"))


EventHandler = Callable[[IncomingMessage], Awaitable[None]]


@runtime_checkable
class Adapter(Protocol):
    name: str  # 协议名（如 "onebot11"），不是产品名
    capabilities: Capabilities

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    async def login_status(self) -> dict[str, Any]: ...

    def on_event(self, handler: EventHandler) -> None: ...

    async def list_groups(self) -> list[dict[str, Any]]: ...

    async def list_members(self, group_id: int) -> list[dict[str, Any]]: ...

    async def list_friends(self) -> list[dict[str, Any]]: ...

    async def fetch_history(self, group_id: int, count: int) -> list[IncomingMessage]: ...

    async def fetch_resource(self, ref: str) -> bytes: ...


def supports(adapter: Any, capability: str) -> bool:
    """能力判定的唯一入口：只读 capabilities。"""
    return capability in (getattr(adapter, "capabilities", None) or ())
