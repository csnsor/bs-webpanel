from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Literal, Optional

import httpx

from ..clients import get_http_client
from ..settings import SUPABASE_KEY, SUPABASE_SESSION_TABLE, SUPABASE_TABLE, SUPABASE_URL, TARGET_GUILD_ID, USERS_TABLE # Added USERS_TABLE
from ..utils import simplify_ban_reason


def is_supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


async def supabase_request(
    method: str,
    table: str,
    *,
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
    prefer: Optional[str] = None,
) -> Optional[Any]:
    if not is_supabase_ready():
        return None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer or "return=representation",
    }
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    try:
        client = get_http_client()
        resp = await client.request(method, url, params=params, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        if not resp.content:
            return True
        return resp.json()
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text or ""
        except Exception:
            body = ""
        logging.warning(
            "Supabase request failed table=%s method=%s status=%s body=%s",
            table,
            method,
            getattr(exc.response, "status_code", "unknown"),
            (body[:800] + "…") if len(body) > 800 else body,
        )
    except Exception as exc:
        logging.warning("Supabase request failed table=%s method=%s error=%s", table, method, exc)
    return None

# --- New user management functions ---

async def get_internal_user_by_platform_id(platform_id: str, platform_type: Literal["discord", "roblox"]) -> Optional[Dict[str, Any]]:
    """Retrieves an internal user record by their Discord or Roblox ID."""
    column_name = f"{platform_type}_id"
    records = await supabase_request(
        "get",
        USERS_TABLE,
        params={column_name: f"eq.{platform_id}", "limit": 1},
    )
    if records:
        return records[0]
    return None

async def create_internal_user(discord_id: Optional[str] = None, roblox_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Creates a new internal user record."""
    payload = {}
    if discord_id:
        payload["discord_id"] = discord_id
    if roblox_id:
        payload["roblox_id"] = roblox_id
    
    if not payload: # Must have at least one ID to create a user
        logging.warning("Attempted to create internal user without any platform IDs.")
        return None

    records = await supabase_request(
        "post",
        USERS_TABLE,
        payload=payload,
        prefer="return=representation", # Return the created record
    )
    if records:
        return records[0]
    return None

async def link_platform_id_to_internal_user(internal_user_id: str, platform_id: str, platform_type: Literal["discord", "roblox"]) -> Optional[Dict[str, Any]]:
    """Links a platform ID to an existing internal user."""
    column_name = f"{platform_type}_id"
    payload = {column_name: platform_id}
    records = await supabase_request(
        "patch",
        USERS_TABLE,
        params={"id": f"eq.{internal_user_id}"},
        payload=payload,
        prefer="return=representation", # Return the updated record
    )
    if records:
        return records[0]
    return None

async def find_or_create_and_link_user(discord_id: Optional[str] = None, roblox_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Finds an existing internal user by Discord or Roblox ID,
    links new IDs to an existing user, or creates a new user.
    Returns the complete user record.
    """
    user_record: Optional[Dict[str, Any]] = None

    # 1. Try to find by Discord ID
    if discord_id:
        user_record = await get_internal_user_by_platform_id(discord_id, "discord")
    
    # 2. If not found by Discord, try to find by Roblox ID
    if not user_record and roblox_id:
        user_record = await get_internal_user_by_platform_id(roblox_id, "roblox")

    # 3. If user found, ensure all provided IDs are linked
    if user_record:
        internal_user_id = user_record["id"]
        # Link Discord ID if not already linked
        if discord_id and not user_record.get("discord_id"):
            user_record = await link_platform_id_to_internal_user(internal_user_id, discord_id, "discord") or user_record
        # Link Roblox ID if not already linked
        if roblox_id and not user_record.get("roblox_id"):
            user_record = await link_platform_id_to_internal_user(internal_user_id, roblox_id, "roblox") or user_record
    else:
        # 4. If no user found, create a new one
        if discord_id or roblox_id:
            user_record = await create_internal_user(discord_id, roblox_id)
        else:
            logging.error("find_or_create_and_link_user called without any platform IDs.")
            return None
    
    return user_record

# --- Existing functions (log_appeal_to_supabase, get_remote_last_submit, etc.) ---

