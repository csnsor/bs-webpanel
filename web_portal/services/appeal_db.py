from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from ..settings import ROBLOX_SUPABASE_TABLE
from .supabase import is_supabase_ready, supabase_request


async def upsert_roblox_appeal(
    roblox_id: str,
    roblox_username: str,
    appeal_text: str,
    ban_data: Dict[str, Any],
    short_ban_reason: str,
    discord_user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Inserts a new Roblox appeal or updates an existing 'pending' one to prevent duplicates.
    This is the primary way user appeal submissions are persisted.
    """
    if not is_supabase_ready():
        return None

    # Idempotency check: Find an existing *pending* appeal for this Roblox ID
    try:
        existing_appeals = await supabase_request(
            "get",
            ROBLOX_SUPABASE_TABLE,
            params={
                "roblox_id": f"eq.{roblox_id}",
                "status": "eq.pending",
                "limit": 1,
            },
        )
        if existing_appeals and isinstance(existing_appeals, list) and len(existing_appeals) > 0:
            appeal_id = existing_appeals[0]['id']
            logging.warning(f"Found existing pending appeal {appeal_id} for Roblox ID {roblox_id}. It will be overwritten.")
        else:
            appeal_id = None
    except Exception as exc:
        logging.error(f"Error checking for existing Roblox appeal for {roblox_id}: {exc}")
        return None

    payload = {
        "roblox_id": roblox_id,
        "roblox_username": roblox_username,
        "appeal_text": appeal_text,
        "ban_data": ban_data,
        "short_ban_reason": short_ban_reason,
        "discord_user_id": discord_user_id,
        "status": "pending",  # Always start as pending
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # If we found an existing appeal, use its ID to update it.
    if appeal_id:
        payload["id"] = appeal_id
        prefer_header = "resolution=merge-duplicates"
    else:
        # This is a new appeal, set created_at
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        prefer_header = "resolution=merge-duplicates"


    try:
        recs = await supabase_request(
            "post",  # POST with 'resolution=merge-duplicates' acts as an upsert
            ROBLOX_SUPABASE_TABLE,
            payload=payload,
            prefer=prefer_header,
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
    Timestamps are now in ISO 8601 format.
    """
    if not is_supabase_ready():
        return None

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "status": status,
        "moderator_id": moderator_id,
        "moderator_username": moderator_username,
        "moderator_action_at": now,
        "updated_at": now,
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
        # Use PATCH to update the record identified by its primary key 'id'
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
    Retrieves a list of Roblox appeals for a given Roblox ID, Discord user ID, or both.
    If both IDs are provided, it fetches records matching either ID.
    """
    if not is_supabase_ready() or (not roblox_id and not discord_user_id):
        return []

    filters = []
    if roblox_id:
        filters.append(f"roblox_id.eq.{roblox_id}")
    if discord_user_id:
        filters.append(f"discord_user_id.eq.{discord_user_id}")

    # Join filters with ',' for an 'OR' condition in Supabase
    or_filter = ",".join(filters)
    
    params = {
        "or": f"({or_filter})",
        "order": "created_at.desc",
        "limit": min(limit, 100),
    }

    try:
        records = await supabase_request("get", ROBLOX_SUPABASE_TABLE, params=params)
        return records if records and isinstance(records, list) else []
    except Exception as exc:
        logging.error(f"Error getting Roblox appeal history for roblox_id={roblox_id}, discord_user_id={discord_user_id}: {exc}")
        return []