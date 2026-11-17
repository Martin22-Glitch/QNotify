"""统计实现（路由由 app/m2.py 注册）。"""

from __future__ import annotations

import time

from fastapi import Depends

from .. import repo
from ..context import AppContext
from ..db import Database
from ..models import COUNTER_MESSAGES_DROPPED
from ..schemas import StatsOut
from .deps import get_ctx, get_db


def local_midnight_ts() -> int:
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def stats(ctx: AppContext = Depends(get_ctx), db: Database = Depends(get_db)) -> StatsOut:
    with db.session() as s:
        total = repo.count_rows(s, "messages")
        today = repo.count_messages_since(s, local_midnight_ts())
        groups = repo.count_rows(s, "groups")
        wl_groups = repo.count_rows(s, "whitelist_groups")
        wl_users = repo.count_rows(s, "whitelist_users")
    return StatsOut(
        messages_total=total,
        messages_today=today,
        messages_dropped=db.get_counter(COUNTER_MESSAGES_DROPPED),
        groups_cached=groups,
        whitelist_groups=wl_groups,
        whitelist_users=wl_users,
        napcat_connected=bool(ctx.napcat and ctx.napcat.connected),
    )
