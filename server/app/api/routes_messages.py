"""GET /api/messages、GET /api/messages/{msg_id}"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import repo
from ..db import Database
from ..schemas import MessageDetail, MessageOut
from .deps import get_db

router = APIRouter()


@router.get("/messages", response_model=list[MessageOut])
def list_messages(
    group_id: int | None = None,
    user_id: int | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = None,
    db: Database = Depends(get_db),
) -> list[MessageOut]:
    with db.session() as s:
        rows = repo.query_messages(
            s, group_id=group_id, user_id=user_id, q=q, limit=limit, before_id=before_id
        )
    return [MessageOut(**r) for r in rows]


@router.get("/messages/{msg_id}", response_model=MessageDetail)
def get_message(msg_id: int, db: Database = Depends(get_db)) -> MessageDetail:
    with db.session() as s:
        row = repo.get_message(s, msg_id)
    if row is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    raw_json = row.pop("raw_json", "")
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError):
        raw = raw_json
    return MessageDetail(**row, raw=raw)
