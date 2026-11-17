"""协议端适配器层：上层只从这里拿名字，不认识任何具体协议端。"""

from app.adapters.base import (
    CAP_FRIENDS,
    CAP_GROUPS,
    CAP_HISTORY,
    CAP_MEMBERS,
    CAP_RESOURCES,
    KNOWN_CAPABILITIES,
    UNSUPPORTED_DETAIL,
    Adapter,
    Capabilities,
    NotSupported,
    supports,
)
from app.adapters.config import AdapterConfig, resolve_adapter_config
from app.adapters.registry import create_adapter, get_adapter, normalize_name

__all__ = [
    "CAP_FRIENDS",
    "CAP_GROUPS",
    "CAP_HISTORY",
    "CAP_MEMBERS",
    "CAP_RESOURCES",
    "KNOWN_CAPABILITIES",
    "UNSUPPORTED_DETAIL",
    "Adapter",
    "AdapterConfig",
    "Capabilities",
    "NotSupported",
    "create_adapter",
    "get_adapter",
    "normalize_name",
    "resolve_adapter_config",
    "supports",
]
