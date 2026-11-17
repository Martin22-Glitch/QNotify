"""Bearer 令牌校验。挂在 /api 路由组上，作为全局依赖。"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status


def token_matches(given: str, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_token(request: Request) -> None:
    expected = request.app.state.ctx.settings.access_token
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token_matches(token.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权：缺少或错误的访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
