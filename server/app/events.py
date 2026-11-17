"""实时事件推送中枢（/ws/events 的发布侧）。

设计要点：
- **线程安全**：业务写入发生在线程池里（`asyncio.to_thread`），所以 `publish()` 可以在任意线程调用，
  内部用 `loop.call_soon_threadsafe` 把发送调度回事件循环。
- **不阻塞业务**：发送是 fire-and-forget，失败只剔除该连接，不影响入库/判定。
- **不缓存**：WebSocket 断开期间的事件不补发（前端重连后会重新拉取一次全量，见前端实现）。
- **带账号**：业务事件（message./todo./review./chat./reminder.）必须带 `account_id`。
  调用方显式给优先；没给就从调用上下文绑定的作用域补（见 accounts.scope_account）；仍缺则告警。

事件命名：`<领域>.<动作>`，如 `message.created`、`todo.created`。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

BUSINESS_PREFIXES = ("message.", "todo.", "review.", "chat.", "reminder.")


class EventHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._account_resolver: Callable[[], int | None] | None = None
        self._warned: set[str] = set()

    # ---------- 生命周期 ----------
    def bind_loop(self) -> None:
        """在事件循环线程里调用一次（应用启动时）。"""
        self._loop = asyncio.get_running_loop()

    def set_account_resolver(self, fn: Callable[[], int | None] | None) -> None:
        """事件没带 account_id 时用它从调用上下文取（必须是纯内存读取，不能查库）。"""
        self._account_resolver = fn

    async def connect(self, ws: WebSocket, hello: dict[str, Any] | None = None) -> None:
        await ws.accept()
        self._clients.add(ws)
        if hello is not None:
            try:
                await ws.send_text(json.dumps(hello, ensure_ascii=False))
            except Exception:  # 客户端刚连就断
                self._clients.discard(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---------- 发布 ----------
    def publish(self, event: dict[str, Any], account_id: int | None = None) -> None:
        """任意线程可调用；没有客户端时是空操作。"""
        if not self._clients or self._loop is None:
            return
        ev = dict(event)
        if ev.get("account_id") is None:
            acc = account_id
            if acc is None and self._account_resolver is not None:
                try:
                    acc = self._account_resolver()
                except Exception:
                    acc = None
            if acc is not None:
                ev["account_id"] = int(acc)
            elif str(ev.get("type", "")).startswith(BUSINESS_PREFIXES):
                t = str(ev.get("type"))
                if t not in self._warned:
                    self._warned.add(t)
                    log.warning("业务事件缺 account_id（前端无法按账号过滤）：type=%s", t)
        payload = json.dumps(ev, ensure_ascii=False, default=str)
        try:
            self._loop.call_soon_threadsafe(self._fanout, payload)
        except RuntimeError:  # 事件循环已关闭（测试/退出）
            return

    def _fanout(self, payload: str) -> None:
        for ws in list(self._clients):
            asyncio.create_task(self._send(ws, payload))

    async def _send(self, ws: WebSocket, payload: str) -> None:
        try:
            await ws.send_text(payload)
        except Exception as exc:  # 断开、超时等：剔除即可，不 raise
            log.debug("推送失败，剔除连接：%s", type(exc).__name__)
            self._clients.discard(ws)


hub = EventHub()
