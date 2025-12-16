from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import httpx
from fastapi import HTTPException

from ..clients import get_http_client
from ..settings import (
    APPEAL_CHANNEL_ID,
    AUTH_LOG_CHANNEL_ID,
    CLEANUP_DM_INVITES,
    DISCORD_API_BASE,
    DISCORD_BOT_TOKEN,
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_REDIRECT_URI,
    DM_GUILD_ID,
    GUILD_NAME_CACHE_TTL_SECONDS,
    OAUTH_SCOPES,
    REMOVE_FROM_DM_GUILD_AFTER_DM,
    ROBLOX_APPEAL_CHANNEL_ID,
    ROBLOX_UNBAN_REQUEST_CHANNEL_ID,
    TARGET_GUILD_ID,
    TARGET_GUILD_NAME,
)
from ..state import _declined_users, _guild_name_cache, _user_tokens


def oauth_authorize_url(state: str) -> str:
    return (
        f"{DISCORD_API_BASE}/oauth2/authorize"
        f"?response_type=code&client_id={DISCORD_CLIENT_ID}"
        f"&scope={OAUTH_SCOPES}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&state={state}"
        f"&prompt=none"
    )


async def exchange_code_for_token(code: str) -> dict:
    try:
        client = get_http_client()
        resp = await client.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logging.warning("OAuth code exchange failed: %s | body=%s", exc, exc.response.text)
        raise HTTPException(status_code=400, detail="Authentication failed. Please try logging in again.") from exc

def store_user_token(user_id: str, token_data: dict) -> None:
    expires_in = float(token_data.get("expires_in") or 0)
    _user_tokens[user_id] = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": time.time() + expires_in - 60 if expires_in else None,
        "token_type": token_data.get("token_type", "Bearer"),
    }


async def refresh_user_token(user_id: str) -> Optional[str]:
    token_data = _user_tokens.get(user_id) or {}
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return None
    try:
        client = get_http_client()
        resp = await client.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        new_token = resp.json()
        store_user_token(user_id, new_token)
        return new_token.get("access_token")
    except Exception as exc:
        logging.warning("Failed to refresh token for user %s: %s", user_id, exc)
        return None


async def get_valid_access_token(user_id: str) -> Optional[str]:
    token_data = _user_tokens.get(user_id) or {}
    access_token = token_data.get("access_token")
    expires_at = token_data.get("expires_at")
    if not access_token:
        return None
    if expires_at and time.time() > expires_at:
        return await refresh_user_token(user_id)
    return access_token


async def fetch_discord_user(access_token: str) -> dict:
    client = get_http_client()
    resp = await client.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_ban_if_exists(user_id: str) -> Optional[dict]:
    client = get_http_client()
    resp = await client.get(
        f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return None


async def fetch_guild_name(guild_id: str) -> Optional[str]:
    if not guild_id or guild_id == "0":
        return None
    if TARGET_GUILD_NAME and str(guild_id) == str(TARGET_GUILD_ID):
        return TARGET_GUILD_NAME

    now = time.time()
    cached = _guild_name_cache.get(str(guild_id))
    if cached and (now - cached[1]) < GUILD_NAME_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        client = get_http_client()
        resp = await client.get(
            f"{DISCORD_API_BASE}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )
        if resp.status_code == 200:
            name = (resp.json() or {}).get("name")
            if name:
                _guild_name_cache[str(guild_id)] = (str(name), now)
                return str(name)
    except Exception:
        pass
    return None


async def ensure_dm_guild_membership(user_id: str) -> bool:
    if not DM_GUILD_ID:
        return False
    if _declined_users.get(user_id):
        return False
    token = await get_valid_access_token(user_id)
    if not token:
        return False
    client = get_http_client()
    resp = await client.put(
        f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"access_token": token},
    )
    added = resp.status_code in (200, 201, 204)
    if added and CLEANUP_DM_INVITES:
        try:
            invite_resp = await client.get(
                f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/invites",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            )
            if invite_resp.status_code == 200:
                for invite in invite_resp.json() or []:
                    code = invite.get("code")
                    if not code:
                        continue
                    await client.delete(
                        f"{DISCORD_API_BASE}/invites/{code}",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    )
            else:
                logging.warning(
                    "Invite cleanup skipped status=%s body=%s",
                    invite_resp.status_code,
                    invite_resp.text,
                )
        except Exception as exc:
            logging.exception("Failed invite cleanup: %s", exc)
    return added


async def maybe_remove_from_dm_guild(user_id: str) -> None:
    if not DM_GUILD_ID or not REMOVE_FROM_DM_GUILD_AFTER_DM:
        return
    client = get_http_client()
    await client.delete(
        f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )


async def remove_from_target_guild(user_id: str) -> Optional[int]:
    client = get_http_client()
    resp = await client.delete(
        f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )
    if resp.status_code not in (200, 204, 404):
        logging.warning("Failed to remove user %s from guild %s: %s %s", user_id, TARGET_GUILD_ID, resp.status_code, resp.text)
    return resp.status_code


async def add_user_to_guild(user_id: str, guild_id: str) -> Optional[int]:
    token = await get_valid_access_token(user_id)
    if not token:
        logging.warning("No OAuth token cached for user %s; cannot re-add to guild %s", user_id, guild_id)
        return None
    client = get_http_client()
    resp = await client.put(
        f"{DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"access_token": token},
    )
    if resp.status_code not in (200, 201, 204):
        logging.warning("Failed to add user %s to guild %s: %s %s", user_id, guild_id, resp.status_code, resp.text)
    return resp.status_code


async def send_log_message(content: str) -> None:
    try:
        client = get_http_client()
        resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{AUTH_LOG_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"content": content},
            timeout=10,
        )
        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(min(retry, 5.0))
            await client.post(
                f"{DISCORD_API_BASE}/channels/{AUTH_LOG_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                json={"content": content},
            )
            return
        resp.raise_for_status()
    except Exception as exc:
        logging.warning("Log post failed: %s", exc)


async def post_appeal_embed(
    appeal_id: str,
    user: dict,
    ban_reason: str,
    ban_evidence: str,
    appeal_reason: str,
) -> None:
    embed = {
        "title": f"Appeal #{appeal_id}",
        "color": 0x3498DB,
        "description": (
            f"**User:** <@{user['id']}> (`{user['username']}#{user.get('discriminator', '0')}`)\n"
            f"**Ban reason:** {ban_reason}\n"
            f"**Evidence:** {ban_evidence}\n"
            f"**Appeal:** {appeal_reason}"
        ),
        "footer": {"text": f"User ID: {user['id']}"},
    }
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Accept",
                    "custom_id": f"web_appeal_accept:{appeal_id}:{user['id']}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Decline",
                    "custom_id": f"web_appeal_decline:{appeal_id}:{user['id']}",
                },
            ],
        }
    ]
    client = get_http_client()
    resp = await client.post(
        f"{DISCORD_API_BASE}/channels/{APPEAL_CHANNEL_ID}/messages",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"embeds": [embed], "components": components},
    )
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Discord is rate limiting. Please retry in a minute.")
    resp.raise_for_status()


async def post_roblox_initial_appeal_embed(
    appeal_id: int,
    roblox_username: str,
    roblox_id: str,
    short_ban_reason: str,
    appeal_reason: str,
    discord_user_id: Optional[str],
) -> Optional[dict]:
    """Posts the initial Roblox appeal embed for the first stage of moderation."""
    embed = {
        "title": f"Roblox Appeal Review (Step 1) #{appeal_id}",
        "color": 0x3498DB,  # Blue for informational
        "description": (
            f"**User:** {roblox_username} (Roblox ID: {roblox_id})\n"
            f"**Discord User:** {f'<@{discord_user_id}>' if discord_user_id else 'N/A'}\n"
            f"**Ban reason:** {short_ban_reason}\n"
            f"**Appeal:** {appeal_reason}"
        ),
        "footer": {"text": f"Appeal ID: {appeal_id}"},
        "url": f"https://www.roblox.com/users/{roblox_id}/profile",
    }
    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Accept (Forward to Final Review)", "custom_id": f"roblox_initial_accept:{appeal_id}"},
            {"type": 2, "style": 4, "label": "Decline", "custom_id": f"roblox_initial_decline:{appeal_id}"},
        ]
    }]
    client = get_http_client()
    try:
        resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{ROBLOX_APPEAL_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed], "components": components},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.error(f"Failed to post initial Roblox appeal embed: {e}")
        return None

async def post_roblox_final_appeal_embed(
    appeal_id: int,
    roblox_username: str,
    roblox_id: str,
    appeal_reason: str,
    initial_moderator_id: str,
) -> Optional[dict]:
    """Posts the final Roblox appeal embed for elevated moderation."""
    embed = {
        "title": f"Roblox Unban Request (Step 2) #{appeal_id}",
        "color": 0xFF0000,  # Red for Roblox
        "description": (
            f"**User:** {roblox_username} (Roblox ID: {roblox_id})\n"
            f"**Appeal:** {appeal_reason}\n\n"
            f"Forwarded for final approval by <@{initial_moderator_id}>."
        ),
        "footer": {"text": f"Appeal ID: {appeal_id}"},
        "url": f"https://www.roblox.com/users/{roblox_id}/profile",
    }
    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Approve Unban", "custom_id": f"roblox_final_accept:{appeal_id}"},
            {"type": 2, "style": 4, "label": "Decline Unban", "custom_id": f"roblox_final_decline:{appeal_id}"},
        ]
    }]
    client = get_http_client()
    try:
        resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{ROBLOX_UNBAN_REQUEST_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed], "components": components},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.error(f"Failed to post final Roblox appeal embed: {e}")
        return None


async def dm_user(user_id: str, embed: dict) -> bool:
    await ensure_dm_guild_membership(user_id)
    client = get_http_client()
    dm = await client.post(
        f"{DISCORD_API_BASE}/users/@me/channels",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"recipient_id": user_id},
    )
    if dm.status_code not in (200, 201):
        return False
    channel_id = dm.json().get("id")
    if not channel_id:
        return False
    resp = await client.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"embeds": [embed]},
    )
    delivered = resp.status_code in (200, 201)
    if delivered:
        await maybe_remove_from_dm_guild(user_id)
    return delivered


async def unban_user_from_guild(user_id: str, guild_id: str) -> bool:
    """
    Unbans a user from a specific Discord guild.
    """
    client = get_http_client()
    try:
        resp = await client.delete(
            f"{DISCORD_API_BASE}/guilds/{guild_id}/bans/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404: # User not banned
            logging.info(f"Attempted to unban user {user_id} from guild {guild_id}, but user was not banned.")
            return True # Consider it successful if they weren't banned in the first place
        logging.error(f"Failed to unban user {user_id} from guild {guild_id}: {exc} - {exc.response.text}")
        return False
    except Exception as exc:
        logging.error(f"Error unbanning user {user_id} from guild {guild_id}: {exc}")
        return False


async def edit_discord_message(
    channel_id: str, message_id: str, embeds: List[dict], components: Optional[List[dict]] = None
) -> Optional[dict]:
    """
    Edits an existing Discord message.
    """
    client = get_http_client()
    payload = {"embeds": embeds}
    if components is not None:
        payload["components"] = components
    try:
        resp = await client.patch(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logging.error(f"Error editing Discord message {message_id} in channel {channel_id}: {exc}")
        return None


