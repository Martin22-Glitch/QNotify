"""配置层。

新增配置项先改本文件，并同步 .env.example 与 README。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QN_",
        env_file=str(SERVER_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    access_token: str = "change-me-before-first-run"
    adapter_ws_url: str = "ws://127.0.0.1:3001/"
    adapter_token: str = ""
    db_path: str = "data/qnotify.db"
    backfill_enabled: bool = True
    backfill_count: int = 50

    # 模型层
    llm_provider: str = "openai"  # openai | anthropic
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # 处理管线
    pipeline_enabled: bool = True
    confidence_threshold: float = 0.7
    # 是否开思考模式（关掉约省一半钱，但要 A/B 看准确率）
    judge_thinking: bool = True
    chat_thinking: bool = True
    aggregate_window_seconds: int = 45
    auto_flush_seconds: float = 5.0

    # 时间与展示
    timezone: str = "Asia/Shanghai"

    # 浏览器跨域白名单（逗号分隔）。前端 dev 服务(5173)与手机访问域名都要在这里。
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    # 本机任意端口都放行（开发时会给多个前端预览分配不同端口）
    cors_origin_regex: str = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"


    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else SERVER_DIR / p

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]
