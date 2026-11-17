"""每个 create_app() 实例独有的运行时上下文（挂在 app.state.ctx 上）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:
    from .db import Database
    from .napcat.client import NapCatClient


@dataclass
class AppContext:
    settings: Settings
    db: Database | None = None
    napcat: NapCatClient | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
