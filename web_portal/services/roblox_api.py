from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException

from ..clients import get_http_client
from ..settings import (
    ROBLOX_API_BASE,
    ROBLOX_BAN_API_KEY,
    ROBLOX_BAN_API_URL,
    ROBLOX_CLIENT_ID,
    ROBLOX_CLIENT_SECRET,
    ROBLOX_OAUTH_SCOPES,
    ROBLOX_OAUTH_TOKENS_TABLE,
    ROBLOX_REDIRECT_URI,
)
from .supabase import supabase_request

logger = logging.getLogger(__name__)


def oauth_authorize_url(state: str) -> str:
    """Generates the Roblox OAuth 2.0 authorization URL."""
    return (
        f"{ROBLOX_API_BASE}/oauth/v1/authorize"
        f"?response_type=code&client_id={ROBLOX_CLIENT_ID}"
        f"&redirect_uri={ROBLOX_REDIRECT_URI}"
        f"&scope={ROBLOX_OAUTH_SCOPES}"
        f"&state={state}"
    )


async def exchange_code_for_token(code: str) -> dict:
    """Exchanges an authorization code for a Roblox access token."""
    try:
        client = get_http_client()
        auth_header = base64.b64encode(f"{ROBLOX_CLIENT_ID}:{ROBLOX_CLIENT_SECRET}".encode()).decode()
        resp = await client.post(
            f"{ROBLOX_API_BASE}/oauth/v1/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": ROBLOX_REDIRECT_URI,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {auth_header}",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Roblox OAuth code exchange failed: %s | body=%s", exc, exc.response.text)
        raise HTTPException(status_code=400, detail="Roblox authentication failed. Please try again.") from exc


async def refresh_roblox_token(user_id: str, refresh_token: str) -> Optional[dict]:
    """Refreshes a Roblox access token using a refresh token."""
    try:
        client = get_http_client()
        auth_header = base64.b64encode(f"{ROBLOX_CLIENT_ID}:{ROBLOX_CLIENT_SECRET}".encode()).decode()
        resp = await client.post(
            f"{ROBLOX_API_BASE}/oauth/v1/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {auth_header}",
            },
        )
        resp.raise_for_status()
        new_token_data = resp.json()
        await store_roblox_token(user_id, new_token_data)
        return new_token_data
    except httpx.HTTPStatusError as exc:
        logger.warning(f"Failed to refresh Roblox token for user {user_id}: {exc.response.status_code} {exc.response.text}")
        return None


async def store_roblox_token(user_id: str, token_data: dict):
    """Stores a Roblox token in the database."""
    expires_in = token_data.get("expires_in", 0)
    payload = {
        "roblox_id": user_id,
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await supabase_request(ROBLOX_OAUTH_TOKENS_TABLE, "post", payload=payload, prefer="resolution=merge-duplicates")

    
async def get_valid_access_token(user_id: str) -> Optional[str]:
    """Retrieves a valid access token for a Roblox user, refreshing if necessary."""
    try:
        records = await supabase_request(
            "get",
            ROBLOX_OAUTH_TOKENS_TABLE,
            params={"roblox_id": f"eq.{user_id}", "limit": 1},
        )
        if not records:
            return None
        
        token_data = records[0]
        expires_at = datetime.fromisoformat(token_data["expires_at"])

        if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            return token_data["access_token"]
        
        refreshed_token = await refresh_roblox_token(user_id, token_data["refresh_token"])
        return refreshed_token.get("access_token") if refreshed_token else None

    except Exception as e:
        logger.exception(f"Error getting valid Roblox access token for {user_id}: {e}")
        return None



async def get_user_info(access_token: str) -> dict:
    """Fetches the authenticated user's info from the Roblox userinfo endpoint."""
    client = get_http_client()
    resp = await client.get(
        f"{ROBLOX_API_BASE}/oauth/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()


async def get_live_ban_status(roblox_id: str) -> Optional[Dict[str, Any]]:
    """Fetches the live game join restriction status for a Roblox user."""
    logger.info(f"Checking live ban status for Roblox ID: {roblox_id}")
    if not ROBLOX_BAN_API_KEY:
        logger.error("ROBLOX_BAN_API_KEY is not set. Cannot check live ban status.")
        return None

    url = f"https://apis.roblox.com/cloud/v2/universes/6765805766/user-restrictions/{roblox_id}"
    headers = {"x-api-key": ROBLOX_BAN_API_KEY}

    try:
        client = get_http_client()
        response = await client.get(url, headers=headers, timeout=10)
        logger.debug(f"Roblox ban status check for {roblox_id} | Status: {response.status_code} | URL: {url}")

        if response.status_code == 404:
            logger.info(f"No user restriction record found for Roblox ID {roblox_id} (404). Not banned.")
            return None

        response.raise_for_status()
        data = response.json()
        logger.debug(f"Roblox ban status response for {roblox_id}: {data}")

        game_join_restriction = data.get("gameJoinRestriction")
        if game_join_restriction and game_join_restriction.get("active"):
            logger.info(f"Active ban found for Roblox ID {roblox_id}.")
            return game_join_restriction

        logger.info(f"No active ban found for Roblox ID {roblox_id} in API response.")
        # Log the full response data for debugging purposes if no active ban is found
        logger.debug(f"Full Roblox API response when no active ban found for {roblox_id}: {data}")
        return None

    except httpx.HTTPStatusError as http_e:
        error_text = http_e.response.text
        logger.error(
            f"RobloxAPI (get_live_ban_status) HTTP Error for {roblox_id}: {http_e.response.status_code} - {error_text}"
        )
        return None
    except Exception as e:
        logger.exception(f"An unexpected error occurred in get_live_ban_status for {roblox_id}:")
        return None


async def unban_user(roblox_id: str) -> bool:
    """
    Deactivates a user's game join restriction (unbans) using a PATCH request.
    Returns True if the user was successfully unbanned or was not banned.
    """
    logger.info(f"Attempting to unban Roblox ID: {roblox_id} via PATCH")
    if not ROBLOX_BAN_API_KEY:
        logger.error("ROBLOX_BAN_API_KEY is not set. Cannot unban user.")
        return False

    url = f"https://apis.roblox.com/cloud/v2/universes/6765805766/user-restrictions/{roblox_id}"
    params = {"updateMask": "gameJoinRestriction"}
    headers = {"x-api-key": ROBLOX_BAN_API_KEY, "Content-Type": "application/json"}
    payload = {"gameJoinRestriction": {"active": False}}

    try:
        client = get_http_client()
        response = await client.patch(url, params=params, json=payload, headers=headers, timeout=15)

        if response.status_code == 200:
            logger.info(f"Successfully unbanned Roblox ID {roblox_id} (200 OK).")
            return True
        if response.status_code == 404:
            logger.info(f"Attempted to unban Roblox ID {roblox_id}, but no restriction was found (404 Not Found).")
            return True

        response.raise_for_status()
        return False
    except httpx.HTTPStatusError as http_e:
        error_text = http_e.response.text
        logger.error(
            f"RobloxAPI (unban_user) HTTP Error for {roblox_id}: {http_e.response.status_code} - {error_text}"
        )
        return False
    except Exception as e:
        logger.exception(f"An unexpected error occurred in unban_user for {roblox_id}:")
        return False


async def get_ban_history(roblox_id: str) -> List[Dict[str, Any]]:
    """Fetches prior ban logs for a Roblox user."""
    logger.info(f"Attempting to get ban history for Roblox ID: {roblox_id}")
    if not ROBLOX_BAN_API_KEY:
        logger.error("ROBLOX_BAN_API_KEY is not set. Cannot fetch ban history.")
        return []
    if not ROBLOX_BAN_API_URL:
        logger.error("ROBLOX_BAN_API_URL is not set. Cannot fetch ban history.")
        return []

    filter_query = f"user=='users/{roblox_id}'"
    url = f"{ROBLOX_BAN_API_URL}:listLogs?filter={filter_query}"
    headers = {"x-api-key": ROBLOX_BAN_API_KEY, "Content-Type": "application/json"}

    try:
        client = get_http_client()
        response = await client.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            logger.info(f"No ban history found for Roblox ID {roblox_id} (404 Not Found).")
            return []
        response.raise_for_status()
        data = response.json()
        return data.get("logs", []) or []
    except httpx.HTTPStatusError as http_e:
        logger.error(f"RobloxAPI (get_ban_history) HTTP Error for {roblox_id}: {http_e.response.status_code} - {http_e.response.text}")
        raise
    except httpx.RequestError as e:
        logger.error(f"RobloxAPI (get_ban_history) Connection Error for {roblox_id}: {e}")
        raise
    except Exception as e:
        logger.exception(f"RobloxAPI (get_ban_history) Unexpected Error for {roblox_id}: {e}")
        return []