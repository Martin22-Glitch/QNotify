"""NapCat 正向 WebSocket 客户端（纯传输层，不含业务判断）。

职责：连接、指数退避重连、动作调用（echo 匹配 Future）、事件分发给外部回调。
连上后调用 get_login_info 记录登录态；meta_event（心跳等）只刷新连接状态，不往下发。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_ACTION_TIMEOUT = 10.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
STABLE_CONNECTION_SECONDS = 30.0
MAX_FRAME_BYTES = 64 * 1024 * 1024


class NapCatClient:
    def __init__(
        self,
        url: str,
        access_token: str = "",
        on_event: EventHandler | None = None,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> None:
        self._url = url
        self._access_token = access_token
        self._on_event = on_event
        self._timeout = action_timeout

        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._connected_evt = asyncio.Event()
        self._login_evt = asyncio.Event()
        self._self_id: int | None = None          # 本次连接是否已登录
        self._last_self_id: int | None = None     # 最后一次登录的号（断开不清，只在内存）
        self._nickname: str | None = None

        self._run_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._stopping = False
        self._ever_connected = False  # 连上过没有：用于决定重连退避要不要封顶（刚启动时 5 秒内重试）
        self._seq = itertools.count(1)
        self._session_tag = uuid.uuid4().hex[:8]
        self.last_event_at: float | None = None

    # ---------- 状态 ----------
    @property
    def url(self) -> str:
        return self._url

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._connected_evt.is_set()

    @property
    def logged_in(self) -> bool:
        return self.connected and self._self_id is not None

    @property
    def self_id(self) -> int | None:
        return self._self_id if self.connected else None

    @property
    def last_self_id(self) -> int | None:
        """作用域第二档：断线/重连期间保持上一次登录的号，直到拿到新登录信息才变。"""
        return self._last_self_id if self._self_id is None else self._self_id

    @property
    def nickname(self) -> str | None:
        return self._nickname if self.connected else None

    async def wait_connected(self, timeout: float) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout)
        except TimeoutError:
            return False
        return self.connected

    async def wait_logged_in(self, timeout: float) -> bool:
        if self.logged_in:
            return True
        try:
            await asyncio.wait_for(self._login_evt.wait(), timeout)
        except TimeoutError:
            return False
        return self.logged_in

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """立即返回；连接在后台任务里进行，不阻塞服务启动。"""
        if self._run_task is not None:
            return
        self._stopping = False
        self._run_task = asyncio.create_task(self._run(), name="napcat-ws")
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="napcat-dispatch")

    async def stop(self) -> None:
        self._stopping = True
        tasks = [t for t in (self._run_task, self._dispatch_task) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2)
        self._mark_disconnected()
        self._run_task = None
        self._dispatch_task = None

    # ---------- 动作调用 ----------
    async def call(
        self, action: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        """发送动作并等待同 echo 的响应。超时/断线/失败一律返回 None，不抛异常。"""
        ws = self._ws
        if ws is None:
            log.warning("NapCat 未连接，动作 %s 未发送", action)
            return None
        echo = f"qn-{self._session_tag}-{next(self._seq)}"
        fut: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        wait_s = timeout if timeout is not None else self._timeout
        try:
            frame = json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False)
            await ws.send(frame)
            resp = await asyncio.wait_for(fut, wait_s)
        except TimeoutError:
            log.warning("NapCat 动作 %s 超时（%.0fs）", action, wait_s)
            return None
        except ConnectionClosed:
            log.warning("NapCat 动作 %s 发送时连接已断开", action)
            return None
        except Exception as exc:
            log.warning("NapCat 动作 %s 失败：%s", action, type(exc).__name__)
            return None
        finally:
            self._pending.pop(echo, None)

        if not isinstance(resp, dict):
            log.warning("NapCat 动作 %s 无有效响应（连接可能已断开）", action)
            return None
        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            log.warning(
                "NapCat 动作 %s 返回失败 retcode=%s wording=%s",
                action,
                resp.get("retcode"),
                resp.get("wording") or resp.get("message"),
            )
            return None
        return resp.get("data")

    # ---------- 内部：连接循环 ----------
    def _headers(self) -> dict[str, str] | None:
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return None

    async def _run(self) -> None:
        delay = BACKOFF_INITIAL
        while not self._stopping:
            try:
                ws = await connect(
                    self._url,
                    additional_headers=self._headers(),
                    open_timeout=10,
                    close_timeout=2,
                    max_size=MAX_FRAME_BYTES,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("连接 NapCat 失败（%s），%.0fs 后重试", type(exc).__name__, delay)
                await asyncio.sleep(delay)
                # 从没连上过（协议端还没起 / 刚重启）时别指数退避到 60 秒，否则要等一分钟才通
                delay = min(delay * 2, BACKOFF_MAX if self._ever_connected else 5.0)
                continue

            self._ever_connected = True
            started = time.monotonic()
            await self._serve(ws)
            if self._stopping:
                break
            if time.monotonic() - started >= STABLE_CONNECTION_SECONDS:
                # 稳定过：视作重新开始，复位退避与 _ever_connected（协议端再重启能 5 秒内重试）
                self._ever_connected = False
                delay = BACKOFF_INITIAL
            else:
                # 连上很快就断：封顶 5 秒，不持续翻倍到 60 秒
                self._ever_connected = True
                delay = min(max(delay, BACKOFF_INITIAL) * 2, 5.0)
            log.warning("NapCat 连接断开，%.0fs 后重连", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    async def _serve(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._connected_evt.set()
        log.info("已连接 NapCat：%s", self._url)
        reader = asyncio.create_task(self._read_loop(ws), name="napcat-reader")
        try:
            await self._refresh_login()
            await reader
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            log.warning("NapCat 连接关闭：code=%s", getattr(exc.rcvd, "code", None))
        except Exception:
            log.exception("NapCat 读循环异常")
        finally:
            if not reader.done():
                reader.cancel()
                await asyncio.wait({reader}, timeout=2)
            self._mark_disconnected()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2)

    async def _refresh_login(self) -> None:
        data = await self.call("get_login_info")
        if isinstance(data, dict) and data.get("user_id") is not None:
            try:
                self._self_id = int(data["user_id"])
            except (TypeError, ValueError):
                self._self_id = None
            self._nickname = str(data.get("nickname") or "")
            if self._self_id is not None:
                self._last_self_id = self._self_id  # 记住"最后登录的号"，断线后作用域靠它兜底
                self._login_evt.set()
                log.info("NapCat 登录态：self_id=%s", self._self_id)
                return
        log.warning("get_login_info 未返回有效登录信息")

    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                self._handle_frame(raw)
        finally:
            self._fail_pending()

    def _handle_frame(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            frame = json.loads(raw)
        except ValueError:
            log.debug("收到无法解析的帧，已忽略")
            return
        if not isinstance(frame, dict):
            return

        # 动作响应：按 echo 匹配，允许乱序
        if "post_type" not in frame and "echo" in frame:
            echo = frame.get("echo")
            fut = self._pending.get(str(echo)) if echo is not None else None
            if fut is not None and not fut.done():
                fut.set_result(frame)
            else:
                log.debug("收到未匹配的响应 echo=%s", echo)
            return

        post_type = frame.get("post_type")
        if post_type is None:
            return
        self.last_event_at = time.time()
        if post_type == "meta_event":
            return
        self._events.put_nowait(frame)

    async def _dispatch_loop(self) -> None:
        """串行把事件交给外部回调，保证到达顺序，且不阻塞读循环。"""
        while True:
            frame = await self._events.get()
            if self._on_event is None:
                continue
            try:
                await self._on_event(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("事件处理失败 post_type=%s", frame.get("post_type"))

    def _fail_pending(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(None)

    def _mark_disconnected(self) -> None:
        self._ws = None
        self._connected_evt.clear()
        self._login_evt.clear()
        self._self_id = None
        self._nickname = None
        self._fail_pending()
