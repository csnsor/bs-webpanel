from __future__ import annotations

import logging
from typing import Optional, Tuple

import httpx

from ..clients import get_http_client
from ..settings import BLOXLINK_API_KEY, BLOXLINK_GUILD_ID

logger = logging.getLogger(__name__)


async def get_discord_id_from_roblox_id(roblox_id: str) -> Optional[str]:
    """
    Fetches a user's Discord ID from their Roblox ID using the Bloxlink API.
    """
    if not BLOXLINK_GUILD_ID:
        logger.warning("BLOXLINK_GUILD_ID is not set. Cannot fetch Discord ID from Roblox ID.")
        return None

    url = f"https://api.blox.link/v4/public/guilds/{BLOXLINK_GUILD_ID}/roblox-to-discord/{roblox_id}"
    headers = {}
    if BLOXLINK_API_KEY:
        headers["api-key"] = BLOXLINK_API_KEY

    try:
        client = get_http_client()
        response = await client.get(url, headers=headers, timeout=10)

        if response.status_code == 404:
            logger.info(f"No Discord user found for Roblox ID {roblox_id} in Bloxlink.")
            return None

        response.raise_for_status()
        data = response.json()
        
        if data.get("success") and data.get("discordID"):
            discord_id = data["discordID"]
            logger.info(f"Found Discord ID {discord_id} for Roblox ID {roblox_id} via Bloxlink.")
            return discord_id
        
        logger.warning(f"Bloxlink API call for Roblox ID {roblox_id} was successful but did not return a Discord ID. Response: {data}")
        return None

    except httpx.HTTPStatusError as http_e:
        logger.error(
            f"Bloxlink API HTTP Error for Roblox ID {roblox_id}: {http_e.response.status_code} - {http_e.response.text}"
        )
        return None
    except Exception as e:
        logger.exception(f"An unexpected error occurred while fetching from Bloxlink for Roblox ID {roblox_id}:")
        return None

async def get_roblox_user_from_discord_id(discord_id: str) -> Optional[Tuple[str, str]]:
    """
    Fetches a user's Roblox ID and username from their Discord ID.
    First, it uses the Bloxlink API to get the Roblox ID.
    Second, it uses the Roblox API to get the username from the Roblox ID.
    """
    if not BLOXLINK_GUILD_ID:
        logger.warning("BLOXLINK_GUILD_ID is not set. Cannot fetch Roblox user from Discord ID.")
        return None

    # Step 1: Get Roblox ID from Bloxlink
    roblox_id: Optional[str] = None
    bloxlink_url = f"https://api.blox.link/v4/public/guilds/{BLOXLINK_GUILD_ID}/discord-to-roblox/{discord_id}"
    headers = {}
    if BLOXLINK_API_KEY:
        headers["Authorization"] = BLOXLINK_API_KEY

    try:
        client = get_http_client()
        response = await client.get(bloxlink_url, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if data.get("robloxID"):
                roblox_id = str(data["robloxID"])
        elif response.status_code == 404:
            logger.info(f"No Roblox user found for Discord ID {discord_id} in Bloxlink.")
            return None
        else:
            response.raise_for_status()

    except httpx.HTTPStatusError as http_e:
        logger.error(
            f"Bloxlink API HTTP Error for Discord ID {discord_id}: {http_e.response.status_code} - {http_e.response.text}"
        )
        return None
    except Exception as e:
        logger.exception(f"An unexpected error occurred while fetching from Bloxlink for Discord ID {discord_id}:")
        return None

    if not roblox_id:
        logger.info(f"Could not resolve Discord ID {discord_id} to a Roblox ID via Bloxlink.")
        return None

    # Step 2: Get Roblox username from Roblox API
    roblox_username: Optional[str] = None
    roblox_api_url = f"https://users.roblox.com/v1/users/{roblox_id}"
    try:
        client = get_http_client()
        response = await client.get(roblox_api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("name"):
            roblox_username = data["name"]

    except httpx.HTTPStatusError as http_e:
        logger.error(
            f"Roblox API HTTP Error for Roblox ID {roblox_id}: {http_e.response.status_code} - {http_e.response.text}"
        )
        # We have the ID, so maybe we can live without the username
        return roblox_id, None
    except Exception:
        logger.exception(f"An unexpected error occurred while fetching username for Roblox ID {roblox_id}:")
        # We have the ID, so maybe we can live without the username
        return roblox_id, None

    if roblox_id and roblox_username:
        logger.info(f"Resolved Discord ID {discord_id} to Roblox user {roblox_username} ({roblox_id}).")
        return roblox_id, roblox_username
    elif roblox_id:
        return roblox_id, None
    else:
        return None
