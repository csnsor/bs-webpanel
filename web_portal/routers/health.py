from __future__ import annotations

from fastapi import APIRouter

from ..bot import bot_client
from ..services.supabase import is_supabase_ready
from ..settings import MESSAGE_CACHE_GUILD_ID, SUPABASE_CONTEXT_TABLE, TARGET_GUILD_ID
from ..state import _bot_task

router = APIRouter()


@router.get("/health")
async def health():
    online = False
    try:
        online = bool(bot_client and getattr(bot_client, "is_ready", lambda: False)())
    except Exception:
        online = False
    bot_task_state = None
    try:
        if _bot_task is None:
            bot_task_state = "not_started"
        elif _bot_task.cancelled():
            bot_task_state = "cancelled"
        elif _bot_task.done():
            bot_task_state = "done"
        else:
            bot_task_state = "running"
    except Exception:
        bot_task_state = "unknown"
    return {
        "ok": True,
        "bot_online": online,
        "bot_task": bot_task_state,
        "target_guild_id": TARGET_GUILD_ID,
        "message_cache_guild_id": MESSAGE_CACHE_GUILD_ID,
        "supabase_ready": is_supabase_ready(),
        "supabase_context_table": SUPABASE_CONTEXT_TABLE,
    }

