from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..settings import ROBLOX_SUPABASE_TABLE
from .supabase import is_supabase_ready, supabase_request


async def upsert_roblox_appeal(
    roblox_id: str,
    roblox_username: str,
    appeal_text: str,
    ban_data: Dict[str, Any],
    short_ban_reason: str,
    discord_user_id: Optional[str] = None,
    appeal_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Inserts a new Roblox appeal or updates an existing one in Supabase.
    """
    payload = {
        "roblox_id": roblox_id,
        "roblox_username": roblox_username,
        "appeal_text": appeal_text,
        "ban_data": ban_data,
        "short_ban_reason": short_ban_reason,
        "discord_user_id": discord_user_id,
    }
    if appeal_id:
        payload["id"] = appeal_id

    # Using 'on_conflict' for upsert based on 'id' if appeal_id is provided,
    # otherwise it's a regular insert. Supabase's prefer header handles this.
    try:
        recs = await supabase_request(
            "post",
            ROBLOX_SUPABASE_TABLE,
            payload=payload,
            prefer="resolution=merge-duplicates",  # This handles upserting
        )
        if recs and isinstance(recs, list) and len(recs) > 0:
            return recs[0]
        return None
    except Exception as exc:
        logging.error(f"Error upserting Roblox appeal: {exc}")
        return None


async def get_roblox_appeal_by_id(appeal_id: int) -> Optional[Dict[str, Any]]:
    """
    Retrieves a Roblox appeal by its ID.
    """
    if not is_supabase_ready():
        return None
    try:
        records = await supabase_request(
            "get",
            ROBLOX_SUPABASE_TABLE,
            params={"id": f"eq.{appeal_id}", "limit": 1},
        )
        if records and isinstance(records, list) and len(records) > 0:
            return records[0]
        return None
    except Exception as exc:
        logging.error(f"Error getting Roblox appeal by ID {appeal_id}: {exc}")
        return None


async def get_roblox_appeal_by_discord_message_id(discord_message_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves a Roblox appeal by the Discord message ID associated with it.
    """
    if not is_supabase_ready():
        return None
    try:
        records = await supabase_request(
            "get",
            ROBLOX_SUPABASE_TABLE,
            params={"discord_message_id": f"eq.{discord_message_id}", "limit": 1},
        )
        if records and isinstance(records, list) and len(records) > 0:
            return records[0]
        return None
    except Exception as exc:
        logging.error(f"Error getting Roblox appeal by Discord message ID {discord_message_id}: {exc}")
        return None


async def update_roblox_appeal_moderation_status(
    appeal_id: int,
    status: str,
    moderator_id: str,
    moderator_username: str,
    discord_message_id: Optional[str] = None,
    discord_guild_id: Optional[str] = None,
    discord_channel_id: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """
    Updates the moderation status and related details of a Roblox appeal.
    """
    if not is_supabase_ready():
        return None

    payload = {
        "status": status,
        "moderator_id": moderator_id,
        "moderator_username": moderator_username,
        "moderator_action_at": int(time.time()),
        "updated_at": int(time.time()),  # Manual update for updated_at
    }
    if discord_message_id:
        payload["discord_message_id"] = discord_message_id
    if discord_guild_id:
        payload["discord_guild_id"] = discord_guild_id
    if discord_channel_id:
        payload["discord_channel_id"] = discord_channel_id
    if is_active is not None:
        payload["is_active"] = is_active

    try:
        recs = await supabase_request(
            "patch",
            ROBLOX_SUPABASE_TABLE,
            params={"id": f"eq.{appeal_id}"},
            payload=payload,
        )
        if recs and isinstance(recs, list) and len(recs) > 0:
            return recs[0]
        return None
    except Exception as exc:
        logging.error(f"Error updating Roblox appeal moderation status for ID {appeal_id}: {exc}")
        return None


async def get_roblox_appeal_history(
    roblox_id: Optional[str] = None, discord_user_id: Optional[str] = None, limit: int = 25
) -> List[Dict[str, Any]]:
    """
    Retrieves a list of Roblox appeals for a given Roblox ID or Discord user ID.
    """
    if not is_supabase_ready():
        return []

    params = {
        "order": "created_at.desc",
        "limit": min(limit, 100),
    }

    if roblox_id and discord_user_id:
        # Supabase doesn't directly support OR in simple params, so we fetch and combine or use 'in' (less ideal for two distinct columns)
        # For simplicity, let's prioritize roblox_id and then discord_user_id, or consider a single query with 'or' using text search on JSONB or rpc for complex queries.
        # For now, let's assume one or the other, or if both, roblox_id takes precedence.
        params["roblox_id"] = f"eq.{roblox_id}"
        # If we need true OR logic, we'd need to make two requests and merge/deduplicate, or use rpc.
        # For now, let's just make sure both can be used.
    elif roblox_id:
        params["roblox_id"] = f"eq.{roblox_id}"
    elif discord_user_id:
        params["discord_user_id"] = f"eq.{discord_user_id}"
    else:
        return [] # No identifier provided

    try:
        records = await supabase_request("get", ROBLOX_SUPABASE_TABLE, params=params)
        return records if records and isinstance(records, list) else []
    except Exception as exc:
        logging.error(f"Error getting Roblox appeal history for roblox_id {roblox_id} or discord_user_id {discord_user_id}: {exc}")
        return []