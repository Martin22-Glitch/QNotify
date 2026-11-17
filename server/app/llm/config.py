"""判定模块的配置对象。"""

from __future__ import annotations

from dataclasses import dataclass, field

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
SUPPORTED_PROVIDERS = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC)

DEFAULT_BASE_URLS: dict[str, str] = {
    PROVIDER_OPENAI: "https://api.openai.com/v1",
    PROVIDER_ANTHROPIC: "https://api.anthropic.com/v1",
}


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "openai"  # "openai" | "anthropic"
    base_url: str = ""  # 空则用该协议默认地址
    # repr=False：防止整个配置对象被打进日志/异常时连带泄露密钥
    api_key: str = field(default="", repr=False)
    model: str = ""

    @property
    def configured(self) -> bool:
        """api_key 与 model 都非空。"""
        return bool((self.api_key or "").strip()) and bool((self.model or "").strip())

    @property
    def normalized_provider(self) -> str:
        return (self.provider or "").strip().lower()

    def resolved_base_url(self) -> str:
        base = (self.base_url or "").strip() or DEFAULT_BASE_URLS.get(self.normalized_provider, "")
        return base.rstrip("/")
