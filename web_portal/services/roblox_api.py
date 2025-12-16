from __future__ import annotations

import base64
import logging
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
    ROBLOX_REDIRECT_URI,
)

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


async def get_user_info(access_token: str) -> dict:
    """Fetches the authenticated user's info from the Roblox userinfo endpoint."""
    client = get_http_client()
    resp = await client.get(
        f"{ROBLOX_API_BASE}/oauth/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()


# Note: The following two functions are adapted from the user-provided aiohttp snippets.
# They now use httpx to align with the project's existing http client.

async def get_live_ban_status(roblox_id: str) -> Optional[Dict[str, Any]]:
    """Fetches the live game join restriction status for a Roblox user."""
    logger.info(f"--- Checking live ban status for Roblox ID: {roblox_id} ---")
    if not ROBLOX_BAN_API_KEY:
        logger.error("ROBLOX_BAN_API_KEY is not set. Cannot check.")
        return None
    if not ROBLOX_BAN_API_URL:
        logger.error("ROBLOX_BAN_API_URL is not set. Cannot check.")
        return None

    url = f"{ROBLOX_BAN_API_URL}/{roblox_id}"
    headers = {"x-api-key": ROBLOX_BAN_API_KEY}
    try:
        async with get_http_client() as client:
            response = await client.get(url, headers=headers, timeout=10)

            logger.info(f"Roblox API Request URL: {url}")
            logger.info(f"Roblox API Response Status: {response.status_code}")
            logger.info(f"Roblox API Response Headers: {response.headers}")
            logger.info(f"Roblox API Response Body: {response.text}")

            if response.status_code == 404:
                logger.info(f"Result: No restriction record found (404).")
                return None
            
            response.raise_for_status()
            data = response.json()

            if not data:
                logger.info("Result: API returned an empty JSON response.")
                return None

            game_join_restriction = data.get("gameJoinRestriction")

            if game_join_restriction:
                logger.info(f"Result: Found 'gameJoinRestriction' object. It is considered a ban.")
                return game_join_restriction
            
            logger.warning(f"Result: No 'gameJoinRestriction' key found in response. This may indicate no active ban.")
            return None
    except Exception as e:
        logger.exception(f"An exception occurred in get_live_ban_status for {roblox_id}:")
        return None


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
        async with get_http_client() as client:
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
