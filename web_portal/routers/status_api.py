from __future__ import annotations

import time

from fastapi import APIRouter, Request

from ..services.sessions import read_user_session
from ..services.supabase import fetch_appeal_history, is_supabase_ready, get_portal_flag
from ..settings import STATUS_DATA_CACHE_TTL_SECONDS
from ..state import _status_data_cache, _announcement_text, _session_epoch
from ..utils import format_timestamp

router = APIRouter()


@router.get("/status/data")
async def status_data(request: Request):
    session = read_user_session(request)
    if not session:
        return {"history": []}
    if not is_supabase_ready():
        return {"history": []}

    uid_str = str(session.get("uid") or "")
    now = time.time()
    cached = _status_data_cache.get(uid_str)
    if cached and (now - cached[1]) < STATUS_DATA_CACHE_TTL_SECONDS:
        return cached[0]

    history = await fetch_appeal_history(
        session["uid"],
        limit=5,
        select="appeal_id,status,created_at,ban_reason",
    )
    slim = [
        {
            "appeal_id": item.get("appeal_id"),
            "status": item.get("status"),
            "created_at": format_timestamp(item.get("created_at")),
            "ban_reason": item.get("ban_reason"),
        }
        for item in history
    ]
    payload = {"history": slim}
    _status_data_cache[uid_str] = (payload, now)
    return payload


@router.get("/live/announcement")
async def live_announcement():
    # Try Supabase-backed flag; fall back to in-memory.
    ann = await get_portal_flag("announcement", None)
    return {
        "announcement": ann if ann is not None else _announcement_text,
        "epoch": _session_epoch,
    }
