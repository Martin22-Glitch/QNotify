"""适配器配置解析与旧名兼容（第 2 节）。

优先级（同一 key 独立解析）：
  1. 进程环境变量：新名 > 旧名
  2. .env 文件：    新名 > 旧名
  3. Settings 上显式设置的字段（model_fields_set）：新字段 > 旧字段
  4. 默认值
先按"来源层级"、再按"新旧名"排序，与 pydantic-settings 自己"环境变量覆盖 .env"的语义一致，
避免 .env 里的新名把测试/命令行用旧名注入的地址顶掉。命中旧名时打 WARNING；不打印任何值。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_ADAPTER = "onebot11"
DEFAULT_WS_URL = "ws://127.0.0.1:3001/"


@dataclass(frozen=True)
class ConfigKey:
    env: str
    attr: str
    default: str


KEY_ADAPTER = ConfigKey("QN_ADAPTER", "adapter", DEFAULT_ADAPTER)
KEY_WS_URL = ConfigKey("QN_ADAPTER_WS_URL", "adapter_ws_url", DEFAULT_WS_URL)
KEY_TOKEN = ConfigKey("QN_ADAPTER_TOKEN", "adapter_token", "")


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    ws_url: str
    token: str = field(default="", repr=False)
    ws_url_source: str = "default"
    token_source: str = "default"


# ---------------------------------------------------------------- .env
def read_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析（不新增依赖）：KEY=VALUE、# 注释、可选引号与 export 前缀。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        key, sep, value = s.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def _env_files(settings: Any) -> list[Path]:
    cfg = getattr(type(settings), "model_config", None) if settings is not None else None
    files = cfg.get("env_file") if isinstance(cfg, Mapping) else None
    if not files:
        return []
    if isinstance(files, (str, os.PathLike)):
        files = [files]
    return [Path(f) for f in files]


def dotenv_for(settings: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in _env_files(settings):
        merged.update(read_dotenv(path))
    return merged


# ---------------------------------------------------------------- 解析
def _plain(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    s = str(value).strip()
    return s or None


def _explicit_attr(settings: Any, attr: str) -> str | None:
    if settings is None or attr not in (getattr(settings, "model_fields_set", None) or set()):
        return None
    return _plain(getattr(settings, attr, None))


def resolve_key(key: ConfigKey, environ: Mapping[str, str], dotenv: Mapping[str, str], settings: Any) -> tuple[str, str]:
    """返回 (值, 来源描述)：环境变量 > .env > Settings 显式值 > 默认。"""
    for tier, source in (("env", environ), (".env", dotenv)):
        v = _plain(source.get(key.env))
        if v is not None:
            return v, f"{tier}:{key.env}"
    v = _explicit_attr(settings, key.attr)
    if v is not None:
        return v, f"settings:{key.attr}"
    return key.default, "default"


def resolve_adapter_config(
    settings: Any = None,
    environ: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str] | None = None,
) -> AdapterConfig:
    env = os.environ if environ is None else environ
    dot = dotenv_for(settings) if dotenv is None else dotenv
    name, _ = resolve_key(KEY_ADAPTER, env, dot, settings)
    url, url_src = resolve_key(KEY_WS_URL, env, dot, settings)
    token, token_src = resolve_key(KEY_TOKEN, env, dot, settings)
    return AdapterConfig(name=name.strip().lower(), ws_url=url, token=token,
                         ws_url_source=url_src, token_source=token_src)


def apply_to_settings(settings: Any, cfg: AdapterConfig) -> None:
    """把解析结果回写到 Settings 字段，让直接读取 Settings 的代码拿到同一个值。"""
    if settings is None:
        return
    fields = getattr(type(settings), "model_fields", None) or {}
    for key, value in ((KEY_WS_URL, cfg.ws_url), (KEY_TOKEN, cfg.token)):
        if key.attr not in fields:
            continue
        current = getattr(settings, key.attr, None)
        if hasattr(current, "get_secret_value"):
            if current.get_secret_value() != value:
                settings.__dict__[key.attr] = type(current)(value)
        elif current != value:
            settings.__dict__[key.attr] = value
