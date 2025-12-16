from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..clients import get_http_client
from ..i18n import translate_text
from ..services.discord_api import (
    add_user_to_guild,
    dm_user,
    maybe_remove_from_dm_guild,
    remove_from_target_guild,
    unban_user_from_guild,
)
from ..services.interactions import (
    build_decision_embed,
    delete_message,
    respond_ephemeral_embed,
    update_message,
    verify_signature,
)
from ..services import appeal_db
from ..services.supabase import fetch_appeal_record, update_appeal_status
from ..settings import (
    APPEAL_LOG_CHANNEL_ID,
    DISCORD_API_BASE,
    DISCORD_BOT_TOKEN,
    INVITE_LINK,
    MODERATOR_ROLE_IDS,
    READD_GUILD_ID,
    TARGET_GUILD_ID,
)
from ..state import _appeal_locked, _declined_users, _processed_appeals
from ..utils import normalize_language

router = APIRouter()


@router.post("/interactions")
async def interactions(request: Request):
    body = await request.body()
    if not verify_signature(request, body):
        logging.warning("Interactions: invalid signature")
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    payload = await request.json()
    if payload["type"] == 1:  # PING
        return JSONResponse({"type": 1})

    if payload["type"] != 3:  # COMPONENT
        return JSONResponse({"type": 4, "data": {"content": "Unsupported interaction type", "flags": 1 << 6}})

    data = payload.get("data", {})
    custom_id = data.get("custom_id", "")
    member = payload.get("member") or {}
    user_obj = member.get("user") or {}
    moderator_id = user_obj.get("id")
    moderator_username = user_obj.get("username", "Unknown Mod")

    logging.info(f"Interaction: custom_id='{custom_id}' moderator={moderator_id}")

    # Permission Check
    user_roles = set(map(int, member.get("roles", [])))
    if not user_roles.intersection(MODERATOR_ROLE_IDS):
        return await respond_ephemeral_embed("Not allowed", "You don't have the required role to handle appeals.", 0xE74C3C)

    # Prevent double-handling
    now = time.time()
    if custom_id in _processed_appeals and (now - _processed_appeals[custom_id]) < 3600:
        return await respond_ephemeral_embed("Already processed", "This appeal has already been handled recently.", 0xF59E0B)

    # Route action
    action_handlers = {
        "web_appeal_accept": handle_discord_accept,
        "web_appeal_decline": handle_discord_decline,
        "roblox_appeal_accept": handle_roblox_accept,
        "roblox_appeal_decline": handle_roblox_decline,
    }

    parts = custom_id.split(":")
    action = parts[0]
    
    if action not in action_handlers:
        return await respond_ephemeral_embed("Invalid action", "This interaction is not supported.")

    # Acknowledge the interaction immediately
    asyncio.create_task(process_and_cleanup(payload, action, parts, moderator_id, moderator_username))
    return JSONResponse({"type": 6}) # DEFERRED_UPDATE_MESSAGE


async def process_and_cleanup(payload, action, parts, moderator_id, moderator_username):
    channel_id = payload["channel_id"]
    message_id = payload["message"]["id"]
    original_embed = (payload["message"].get("embeds") or [{}])[0]

    try:
        # Show processing state
        processing_embed = copy.deepcopy(original_embed)
        processing_embed["title"] = (processing_embed.get("title") or "Appeal") + " (Processing)"
        processing_embed["color"] = 0xF59E0B
        await update_message(channel_id, message_id, embeds=[processing_embed], components=[])

        handler = {
            "web_appeal_accept": handle_discord_accept,
            "web_appeal_decline": handle_discord_decline,
            "roblox_appeal_accept": handle_roblox_accept,
            "roblox_appeal_decline": handle_roblox_decline,
        }.get(action)

        if not handler:
            return

        final_embed, error = await handler(parts, moderator_id, moderator_username, original_embed)

        if error:
            final_embed = copy.deepcopy(original_embed)
            final_embed["title"] = (final_embed.get("title") or "Appeal") + " (Error)"
            final_embed["color"] = 0xE74C3C
            final_embed["fields"] = (final_embed.get("fields", [])) + [{"name": "Error", "value": error, "inline": False}]
        
        await update_message(channel_id, message_id, embeds=[final_embed], components=[]) # Disable buttons

    except Exception as exc:
        logging.exception(f"Failed to process interaction {action}: {exc}")
        error_embed = copy.deepcopy(original_embed)
        error_embed["title"] = (error_embed.get("title") or "Appeal") + " (Failed)"
        error_embed["color"] = 0xE74C3C
        error_embed["fields"] = (error_embed.get("fields", [])) + [{"name": "Error", "value": "An unexpected error occurred.", "inline": False}]
        await update_message(channel_id, message_id, embeds=[error_embed], components=[])


def updated_embed(original_embed: dict, status: str, moderator_id: str, note: Optional[str] = None) -> dict:
    embed = copy.deepcopy(original_embed)
    status_map = {"accepted": ("Accepted", 0x2ECC71), "declined": ("Declined", 0xE74C3C)}
    label, color = status_map.get(status, ("Unknown", 0x95A5A6))

    embed["color"] = color
    embed["title"] = (embed.get("title", "Appeal")) + f" ({label.upper()})"
    
    # Remove old action fields if they exist
    embed["fields"] = [f for f in embed.get("fields", []) if f.get("name") not in ("Action Taken", "Notes")]
    
    embed["fields"].append({"name": "Action Taken", "value": f"{label} by <@{moderator_id}>", "inline": False})
    if note:
        embed["fields"].append({"name": "Notes", "value": note, "inline": False})
    return embed


async def handle_roblox_accept(parts: list, moderator_id: str, moderator_username: str, original_embed: dict) -> Tuple[dict, Optional[str]]:
    try:
        appeal_id = int(parts[1])
    except (ValueError, IndexError):
        return original_embed, "Invalid appeal ID in interaction."

    appeal = await appeal_db.get_roblox_appeal_by_id(appeal_id)
    if not appeal:
        return original_embed, f"Roblox appeal with ID {appeal_id} not found."
    if appeal.get("status") != "pending":
        return updated_embed(original_embed, appeal['status'], appeal.get('moderator_id', 'Unknown'), "Already handled."), None

    discord_user_id = appeal.get("discord_user_id")
    unban_success = False
    if discord_user_id:
        unban_success = await unban_user_from_guild(discord_user_id, TARGET_GUILD_ID)

    await appeal_db.update_roblox_appeal_moderation_status(
        appeal_id=appeal_id,
        status="accepted",
        moderator_id=moderator_id,
        moderator_username=moderator_username,
        is_active=False,
    )
    
    dm_success = False
    if discord_user_id:
        dm_success = await dm_user(
            discord_user_id,
            {"title": "Roblox Appeal Accepted", "description": "Your appeal for the Roblox ban has been accepted. You have been unbanned from the Discord server.", "color": 0x2ECC71},
        )

    note = f"Discord unban {'successful' if unban_success else 'failed or not applicable'}. DM {'sent' if dm_success else 'failed'}."
    return updated_embed(original_embed, "accepted", moderator_id, note), None

async def handle_roblox_decline(parts: list, moderator_id: str, moderator_username: str, original_embed: dict) -> Tuple[dict, Optional[str]]:
    try:
        appeal_id = int(parts[1])
    except (ValueError, IndexError):
        return original_embed, "Invalid appeal ID in interaction."

    appeal = await appeal_db.get_roblox_appeal_by_id(appeal_id)
    if not appeal:
        return original_embed, f"Roblox appeal with ID {appeal_id} not found."
    if appeal.get("status") != "pending":
        return updated_embed(original_embed, appeal['status'], appeal.get('moderator_id', 'Unknown'), "Already handled."), None
        
    await appeal_db.update_roblox_appeal_moderation_status(
        appeal_id=appeal_id,
        status="declined",
        moderator_id=moderator_id,
        moderator_username=moderator_username,
    )

    discord_user_id = appeal.get("discord_user_id")
    dm_success = False
    if discord_user_id:
        dm_success = await dm_user(
            discord_user_id,
            {"title": "Roblox Appeal Declined", "description": "Your appeal for the Roblox ban has been reviewed and declined.", "color": 0xE74C3C},
        )

    note = f"DM {'sent' if dm_success else 'failed or not applicable'}."
    return updated_embed(original_embed, "declined", moderator_id, note), None


async def handle_discord_accept(parts: list, moderator_id: str, moderator_username: str, original_embed: dict) -> Tuple[dict, Optional[str]]:
    try:
        _, appeal_id, user_id = parts
    except ValueError:
        return original_embed, "Invalid custom ID format for Discord appeal."

    # This logic is adapted from the original `handle_accept`
    appeal_record = await fetch_appeal_record(appeal_id)
    user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))
    
    unban_success = await unban_user_from_guild(user_id, TARGET_GUILD_ID)
    readd_success = await add_user_to_guild(user_id, READD_GUILD_ID)
    
    accept_desc_en = "Your appeal has been reviewed and accepted. You have been unbanned and re-added to the server."
    accept_desc = await translate_text(accept_desc_en, target_lang=user_lang, source_lang="en") if user_lang != "en" else accept_desc_en
    dm_delivered = await dm_user(user_id, {"title": "Appeal Accepted", "description": accept_desc, "color": 0x2ECC71})

    await update_appeal_status(
        appeal_id=appeal_id,
        status="accepted",
        moderator_id=moderator_id,
        dm_delivered=dm_delivered,
    )
    
    note = f"Unban {'OK' if unban_success else 'Fail'}; Re-add {'OK' if readd_success else 'Fail'}; DM {'OK' if dm_delivered else 'Fail'}."
    return updated_embed(original_embed, "accepted", moderator_id, note), None


async def handle_discord_decline(parts: list, moderator_id: str, moderator_username: str, original_embed: dict) -> Tuple[dict, Optional[str]]:
    try:
        _, appeal_id, user_id = parts
    except ValueError:
        return original_embed, "Invalid custom ID format for Discord appeal."

    # This logic is adapted from the original `handle_decline`
    _declined_users[user_id] = True
    _appeal_locked[user_id] = True

    appeal_record = await fetch_appeal_record(appeal_id)
    user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))

    decline_desc_en = "Your appeal has been reviewed and declined. Further appeals are blocked for this ban."
    decline_desc = await translate_text(decline_desc_en, target_lang=user_lang, source_lang="en") if user_lang != "en" else decline_desc_en
    dm_delivered = await dm_user(user_id, {"title": "Appeal Declined", "description": decline_desc, "color": 0xE74C3C})

    await update_appeal_status(
        appeal_id=appeal_id,
        status="declined",
        moderator_id=moderator_id,
        dm_delivered=dm_delivered,
    )
    
    await maybe_remove_from_dm_guild(user_id)
    
    note = f"User has been notified by DM (delivered: {dm_delivered})."
    return updated_embed(original_embed, "declined", moderator_id, note), None

