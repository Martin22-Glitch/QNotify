"""适配器注册表：名字 → 实现类，全部数据驱动（不写 if name == ...）。"""

from __future__ import annotations

import logging
from typing import Any

from app.adapters.base import Adapter, Capabilities
from app.adapters.config import DEFAULT_ADAPTER, AdapterConfig
from app.adapters.onebot11 import OneBot11Adapter

log = logging.getLogger(__name__)

ADAPTER_TYPES: dict[str, Any] = {OneBot11Adapter.name: OneBot11Adapter}

_BOUND: dict[int, Any] = {}  # id(ctx) -> 已包装的适配器


def normalize_name(name: str | None) -> str:
    """未知名字记 ERROR 并按默认适配器处理，绝不因此启动失败。"""
    key = (name or "").strip().lower() or DEFAULT_ADAPTER
    if key not in ADAPTER_TYPES:
        log.error("未知的 QN_ADAPTER=%r，已按 %s 处理", name, DEFAULT_ADAPTER)
        return DEFAULT_ADAPTER
    return key


def adapter_type(name: str | None) -> Any:
    return ADAPTER_TYPES.get((name or "").strip().lower()) or ADAPTER_TYPES[DEFAULT_ADAPTER]


def default_capabilities(name: str | None) -> Capabilities:
    return Capabilities(adapter_type(name).DEFAULT_CAPABILITIES)


def create_adapter(cfg: AdapterConfig) -> Adapter:
    """按配置新建一个自持连接的适配器（供 main.py 迁移后使用）。"""
    return adapter_type(normalize_name(cfg.name)).from_config(cfg)


def get_adapter(ctx: Any, name: str | None = None) -> Adapter | None:
    """取当前上下文的适配器：包装上下文里已在运行的传输客户端；找不到返回 None。"""
    if ctx is None:
        return None
    cls = adapter_type(name)
    adapter = cls.attach_existing(ctx, _BOUND.get(id(ctx)))
    if adapter is None:
        _BOUND.pop(id(ctx), None)
        return None
    _BOUND[id(ctx)] = adapter
    return adapter


def reconfigure_existing(ctx: Any, cfg: AdapterConfig) -> bool:
    return bool(adapter_type(cfg.name).reconfigure_existing(ctx, cfg))
