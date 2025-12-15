from __future__ import annotations

import logging
import time
from typing import List

from ..settings import (
    BOT_EVENT_LOGGING,
    DEBUG_EVENTS,
    ENABLE_MESSAGE_SNAPSHOTS,
    MESSAGE_CACHE_GUILD_ID,
    RECENT_MESSAGE_CACHE_TTL,
    SUPABASE_CONTEXT_TABLE,
)
from ..state import _message_buffer, _recent_message_context
from .supabase import is_supabase_ready, supabase_request

# Only track messages from the single configured cache guild.
MESSAGE_CACHE_GUILD_IDS = {str(MESSAGE_CACHE_GUILD_ID)}


def should_track_messages(guild_id: int | str) -> bool:
    return str(guild_id) in MESSAGE_CACHE_GUILD_IDS


def truncate_log_text(value: str, limit: int = 260) -> str:
    value = (value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


async def maybe_snapshot_messages(user_id: str, guild_id: str):
    if not ENABLE_MESSAGE_SNAPSHOTS:
        return
    if not is_supabase_ready():
        return
    if not should_track_messages(guild_id):
        logging.debug("Message caching skipped for guild %s", guild_id)
        return
    entries = list(_message_buffer.get(user_id, []))
    if not entries:
        return
    if BOT_EVENT_LOGGING and DEBUG_EVENTS:
        logging.info("[snapshot] user=%s guild=%s msgs=%s", user_id, guild_id, len(entries[-15:]))
    await persist_message_snapshot(user_id, entries[-15:])


async def persist_message_snapshot(user_id: str, messages: List[dict]):
    if not is_supabase_ready() or not messages:
        return
    logging.info("Persisting %d messages for user %s", len(messages[-15:]), user_id)
    try:
        updated_at = int(time.time())
        await supabase_request(
            "post",
            "user_message_snapshots",
            params={"on_conflict": "user_id"},
            payload={"user_id": user_id, "messages": messages[-15:], "updated_at": updated_at},
            prefer="resolution=merge-duplicates,return=minimal",
        )
    except Exception as exc:
        logging.warning("Snapshot persist failed for %s: %s", user_id, exc)


async def fetch_message_cache(user_id: str, limit: int = 15) -> List[dict]:
    if not is_supabase_ready():
        return _get_recent_message_context(user_id, limit)
    try:
        recs = await supabase_request(
            "get",
            SUPABASE_CONTEXT_TABLE,
            params={"user_id": f"eq.{user_id}", "limit": 1, "select": "messages"},
        )
        if recs and recs[0].get("messages"):
            messages = recs[0]["messages"]

            def get_ts(m: dict) -> float:
                t = m.get("timestamp", 0)
                try:
                    return float(t)
                except Exception:
                    return 0.0

            return sorted(messages, key=get_ts, reverse=True)[:limit]
    except Exception as exc:
        logging.warning("Failed to fetch context for %s: %s", user_id, exc)
    return _get_recent_message_context(user_id, limit)


def _get_recent_message_context(user_id: str, limit: int) -> List[dict]:
    entry = _recent_message_context.get(user_id)
    if not entry:
        return []
    messages, ts = entry
    if time.time() - ts > RECENT_MESSAGE_CACHE_TTL:
        _recent_message_context.pop(user_id, None)
        return []

    def _timestamp_value(msg: dict) -> float:
        try:
            return float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0.0

    return sorted(messages, key=_timestamp_value, reverse=True)[:limit]

