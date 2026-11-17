"""GET /api/login/status"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..context import AppContext
from ..schemas import LoginStatus
from .deps import get_ctx

router = APIRouter()


@router.get("/login/status", response_model=LoginStatus)
def login_status(ctx: AppContext = Depends(get_ctx)) -> LoginStatus:
    client = ctx.napcat
    if client is None or not client.logged_in:
        return LoginStatus(logged_in=False, self_id=None, nickname=None)
    return LoginStatus(logged_in=True, self_id=client.self_id, nickname=client.nickname)
