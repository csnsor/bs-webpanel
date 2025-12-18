from __future__ import annotations

import asyncio
import copy
import logging
from typing import Optional, Tuple, Dict, Callable, Coroutine, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services import appeal_db, roblox_api
from ..services.discord_api import (
    add_user_to_guild,
    dm_user,
    maybe_remove_from_dm_guild,
    post_roblox_final_appeal_embed,
    remove_from_target_guild,
    unban_user_from_guild,
    delete_message,
    post_channel_message,
)
from ..services.interactions import respond_ephemeral_embed, update_message, verify_signature
from ..services.supabase import fetch_appeal_record, update_appeal_status, update_staff_stats, fetch_reports_for_roblox_id
from ..settings import (
    DISCORD_MODERATOR_ROLE_ID,
    READD_GUILD_ID,
    ROBLOX_ELEVATED_MODERATOR_ROLE_ID,
    ROBLOX_INITIAL_MODERATOR_ROLE_ID,
    TARGET_GUILD_ID,
)
from ..state import _appeal_locked, _declined_users
from ..i18n import translate_text
from ..utils import normalize_language

router = APIRouter()
FINAL_LOG_CHANNEL_ID = "1353445286457901106"

# --- Helper Functions ---

def create_updated_embed(
    original_embed: dict, status: str, moderator_id: str, note: Optional[str] = None
) -> dict:
    """Creates an updated embed reflecting the moderation action."""
    embed = copy.deepcopy(original_embed)
    status_map = {
        "accepted": ("Accepted", 0x2ECC71),
        "declined": ("Declined", 0xE74C3C),
        "forwarded": ("Forwarded for Final Review", 0x3498DB),
    }
    label, color = status_map.get(status, ("Unknown", 0x95A5A6))

    embed["title"] = f"{embed.get('title', 'Appeal')} ({label.upper()})"
    embed["color"] = color
    
    embed["fields"] = [f for f in embed.get("fields", []) if f.get("name") not in ("Action Taken", "Notes")]
    
    embed["fields"].append({"name": "Action Taken", "value": f"{label} by <@{moderator_id}>", "inline": False})
    if note:
        embed["fields"].append({"name": "Notes", "value": note, "inline": False})
        
    return embed


def _extract_evidence_links(reports: Optional[list]) -> list:
    """Extract only URL tokens from report evidence strings."""
    links = []
    seen = set()
    for report in reports or []:
        evidence = str(report.get("evidence") or "")
        tokens = evidence.replace("\n", " ").split()
        for token in tokens:
            if token.startswith("http://") or token.startswith("https://"):
                if token not in seen:
                    seen.add(token)
                    links.append(token)
    return links

# --- Roblox Appeal Handlers ---

async def handle_roblox_initial_accept(parts: list, mod_id: str, mod_name: str, embed: dict, payload: dict) -> Tuple[dict, Optional[str], Optional[dict]]:
    appeal_id = int(parts[1])
    appeal = await appeal_db.get_roblox_appeal_by_id(appeal_id)
    if not appeal:
        return embed, f"Appeal {appeal_id} not found."
    if appeal["status"] != "pending":
        return embed, "This appeal has already been processed."

    await appeal_db.update_roblox_appeal_moderation_status(appeal_id, "pending_elevation", mod_id, mod_name)

    reports = await fetch_reports_for_roblox_id(appeal["roblox_id"], limit=25)
    evidence_links = _extract_evidence_links(reports)

    new_message = await post_roblox_final_appeal_embed(
        appeal_id=appeal_id,
        roblox_username=appeal["roblox_username"],
        roblox_id=appeal["roblox_id"],
        appeal_reason=appeal["appeal_text"],
        initial_moderator_id=mod_id,
        short_ban_reason=appeal.get("short_ban_reason"),
        discord_user_id=appeal.get("discord_user_id"),
        evidence_links=evidence_links,
    )

    if new_message and new_message.get("id"):
        await appeal_db.update_roblox_appeal_moderation_status(appeal_id, "pending_elevation", mod_id, mod_name, discord_message_id=new_message["id"])
    
    # Delete the initial message
    await delete_message(payload["channel_id"], payload["message"]["id"])

    return create_updated_embed(embed, "forwarded", mod_id, "Forwarded for final review."), None, None

async def handle_roblox_initial_decline(parts: list, mod_id: str, mod_name: str, embed: dict, payload: dict) -> Tuple[dict, Optional[str], Optional[dict]]:
    appeal_id = int(parts[1])
    appeal = await appeal_db.get_roblox_appeal_by_id(appeal_id)
    if not appeal:
        return embed, f"Appeal {appeal_id} not found."
    if appeal["status"] != "pending":
        return embed, "This appeal has already been processed."

    await appeal_db.update_roblox_appeal_moderation_status(appeal_id, "declined", mod_id, mod_name, is_active=False)
    if appeal.get("internal_user_id"):
        _appeal_locked[appeal["internal_user_id"]] = True
    
    if appeal.get("discord_user_id"):
        await dm_user(appeal["discord_user_id"], {"title": "Roblox Appeal Declined", "description": "Your appeal has been reviewed and declined.", "color": 0xE74C3C})
        await maybe_remove_from_dm_guild(appeal["discord_user_id"])
    
    # Delete the initial message and log it
    await delete_message(payload["channel_id"], payload["message"]["id"])
    logging.info(f"Roblox appeal {appeal_id} declined by {mod_name} ({mod_id}) and message deleted.")
    await update_staff_stats(mod_id, mod_name, accepted=False, created_at=appeal.get("created_at"))
        
    return create_updated_embed(embed, "declined", mod_id), None, None

async def handle_roblox_final_accept(parts: list, mod_id: str, mod_name: str, embed: dict, payload: dict) -> Tuple[dict, Optional[str], Optional[dict]]:
    appeal_id = int(parts[1])
    appeal = await appeal_db.get_roblox_appeal_by_id(appeal_id)
    if not appeal:
        return embed, f"Appeal {appeal_id} not found."
    if appeal["status"] != "pending_elevation":
        return embed, "This appeal is not pending final review."

    reports = await fetch_reports_for_roblox_id(appeal["roblox_id"], limit=25)
    evidence_links = _extract_evidence_links(reports)
    evidence_text = "\n".join(evidence_links[:8]) if evidence_links else "No evidence links found."
    discord_label = f"<@{appeal.get('discord_user_id')}>" if appeal.get("discord_user_id") else "Not linked"

    unban_success = await roblox_api.unban_user(appeal["roblox_id"])
    if not unban_success:
        return create_updated_embed(embed, "declined", mod_id, "Failed to unban from Roblox via API."), "Roblox API unban failed."

    await appeal_db.update_roblox_appeal_moderation_status(appeal_id, "accepted", mod_id, mod_name, is_active=False)
    if appeal.get("internal_user_id"):
        _appeal_locked[appeal["internal_user_id"]] = True
    await update_staff_stats(mod_id, mod_name, accepted=True, created_at=appeal.get("created_at"))
    
    if appeal.get("discord_user_id"):
        await dm_user(appeal["discord_user_id"], {"title": "Roblox Appeal Accepted", "description": "Your Roblox appeal has been accepted and you have been unbanned.", "color": 0x2ECC71})
        await maybe_remove_from_dm_guild(appeal["discord_user_id"])
        
    log_embed = {
        "title": f"Roblox Appeal Accepted ({appeal_id})",
        "description": (
            f"**Appealing Player:** {appeal['roblox_username']} ({appeal['roblox_id']})\n"
            f"**Discord:** {discord_label}\n"
            f"**Ban reason:** {appeal['short_ban_reason']}\n"
            f"**Appeal:** {appeal['appeal_text']}\n"
            f"**Evidence:** {evidence_text}"
        ),
        "color": 0x2ECC71,
        "fields": [
            {"name": "Moderator", "value": f"<@{mod_id}> ({mod_name})", "inline": False},
        ],
    }
    meta = {
        "delete_message": True,
        "ephemeral": "Appeal accepted, user unbanned, and review message removed.",
        "log_channel": FINAL_LOG_CHANNEL_ID,
        "log_embed": log_embed,
    }
    return create_updated_embed(embed, "accepted", mod_id, "User unbanned from Roblox."), None, meta

async def handle_roblox_final_decline(parts: list, mod_id: str, mod_name: str, embed: dict, payload: dict) -> Tuple[dict, Optional[str], Optional[dict]]:
    appeal_id = int(parts[1])
    appeal = await appeal_db.get_roblox_appeal_by_id(appeal_id)
    if not appeal:
        return embed, f"Appeal {appeal_id} not found."
    if appeal["status"] != "pending_elevation":
        return embed, "This appeal is not pending final review."

    await appeal_db.update_roblox_appeal_moderation_status(appeal_id, "declined", mod_id, mod_name, is_active=False)
    if appeal.get("internal_user_id"):
        _appeal_locked[appeal["internal_user_id"]] = True
    await update_staff_stats(mod_id, mod_name, accepted=False, created_at=appeal.get("created_at"))
    
    if appeal.get("discord_user_id"):
        await dm_user(appeal["discord_user_id"], {"title": "Roblox Appeal Declined", "description": "Your Roblox appeal was declined during final review.", "color": 0xE74C3C})
        await maybe_remove_from_dm_guild(appeal["discord_user_id"])
        
    meta = {"delete_message": True, "ephemeral": "Appeal declined and review message removed."}
    return create_updated_embed(embed, "declined", mod_id), None, meta

# --- Discord Appeal Handlers (Legacy) ---

async def handle_discord_accept(parts: list, mod_id: str, mod_name: str, embed: dict, payload: dict) -> Tuple[dict, Optional[str], Optional[dict]]:
    _, appeal_id, user_id = parts
    appeal_record = await fetch_appeal_record(appeal_id)
    user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))
    internal_user_id = (appeal_record or {}).get("internal_user_id") or user_id
    
    unban_success = await unban_user_from_guild(user_id, TARGET_GUILD_ID)
    readd_success = await add_user_to_guild(user_id, READD_GUILD_ID)
    
    accept_desc_en = "Your appeal has been reviewed and accepted. You have been unbanned and re-added to the server."
    accept_desc = await translate_text(accept_desc_en, target_lang=user_lang) if user_lang != "en" else accept_desc_en
    dm_delivered = await dm_user(user_id, {"title": "Appeal Accepted", "description": accept_desc, "color": 0x2ECC71})
    await maybe_remove_from_dm_guild(user_id)

    await update_appeal_status(appeal_id, "accepted", mod_id, dm_delivered=dm_delivered)
    _appeal_locked[internal_user_id] = True
    await update_staff_stats(mod_id, mod_name, accepted=True, created_at=(appeal_record or {}).get("created_at"))
    
    note = f"Unban {'OK' if unban_success else 'Fail'}; Re-add {'OK' if readd_success else 'Fail'}; DM {'OK' if dm_delivered else 'Fail'}."
    log_embed = {
        "title": f"Discord Appeal Accepted ({appeal_id})",
        "description": (
            f"**User:** <@{user_id}> ({user_id})\n"
            f"**Ban reason:** {(appeal_record or {}).get('ban_reason') or 'N/A'}\n"
            f"**Appeal:** {(appeal_record or {}).get('appeal_reason') or 'N/A'}"
        ),
        "color": 0x2ECC71,
        "fields": [
            {"name": "Moderator", "value": f"<@{mod_id}> ({mod_name})", "inline": False},
            {"name": "DM delivered", "value": str(dm_delivered), "inline": True},
        ],
    }
    meta = {
        "delete_message": True,
        "ephemeral": "Appeal accepted, user unbanned, message removed.",
        "log_channel": FINAL_LOG_CHANNEL_ID,
        "log_embed": log_embed,
    }
    return create_updated_embed(embed, "accepted", mod_id, note), None, meta

async def handle_discord_decline(parts: list, mod_id: str, mod_name: str, embed: dict, payload: dict) -> Tuple[dict, Optional[str], Optional[dict]]:
    _, appeal_id, user_id = parts
    appeal_record = await fetch_appeal_record(appeal_id)
    internal_user_id = (appeal_record or {}).get("internal_user_id") or user_id
    _declined_users[internal_user_id] = True
    _appeal_locked[internal_user_id] = True

    user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))

    decline_desc_en = "Your appeal has been reviewed and declined. Further appeals are blocked for this ban."
    decline_desc = await translate_text(decline_desc_en, target_lang=user_lang) if user_lang != "en" else decline_desc_en
    dm_delivered = await dm_user(user_id, {"title": "Appeal Declined", "description": decline_desc, "color": 0xE74C3C})

    await update_appeal_status(appeal_id, "declined", mod_id, dm_delivered=dm_delivered)
    await maybe_remove_from_dm_guild(user_id)
    await update_staff_stats(mod_id, mod_name, accepted=False, created_at=(appeal_record or {}).get("created_at"))
    
    note = f"User has been notified by DM (delivered: {dm_delivered})."
    log_embed = {
        "title": f"Discord Appeal Declined ({appeal_id})",
        "description": (
            f"**User:** <@{user_id}> ({user_id})\n"
            f"**Ban reason:** {(appeal_record or {}).get('ban_reason') or 'N/A'}\n"
            f"**Appeal:** {(appeal_record or {}).get('appeal_reason') or 'N/A'}"
        ),
        "color": 0xE74C3C,
        "fields": [
            {"name": "Moderator", "value": f"<@{mod_id}> ({mod_name})", "inline": False},
            {"name": "DM delivered", "value": str(dm_delivered), "inline": True},
        ],
    }
    meta = {
        "delete_message": True,
        "ephemeral": "Appeal declined and message removed.",
        "log_channel": FINAL_LOG_CHANNEL_ID,
        "log_embed": log_embed,
    }
    return create_updated_embed(embed, "declined", mod_id, note), None, meta

# --- Main Interaction Router ---

HANDLER_MAP: Dict[str, Tuple[Callable, int]] = {
    "web_appeal_accept": (handle_discord_accept, DISCORD_MODERATOR_ROLE_ID),
    "web_appeal_decline": (handle_discord_decline, DISCORD_MODERATOR_ROLE_ID),
    "roblox_initial_accept": (handle_roblox_initial_accept, ROBLOX_INITIAL_MODERATOR_ROLE_ID),
    "roblox_initial_decline": (handle_roblox_initial_decline, ROBLOX_INITIAL_MODERATOR_ROLE_ID),
    "roblox_final_accept": (handle_roblox_final_accept, ROBLOX_ELEVATED_MODERATOR_ROLE_ID),
    "roblox_final_decline": (handle_roblox_final_decline, ROBLOX_ELEVATED_MODERATOR_ROLE_ID),
}

@router.post("/interactions")
async def interactions(request: Request):
    body = await request.body()
    if not verify_signature(request, body):
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    payload = await request.json()
    if payload["type"] == 1:
        return JSONResponse({"type": 1})
    if payload["type"] != 3:
        return JSONResponse({"error": "Unsupported interaction type"}, status_code=400)

    data = payload.get("data", {})
    custom_id = data.get("custom_id", "")
    parts = custom_id.split(":")
    action = parts[0]

    handler, required_role = HANDLER_MAP.get(action, (None, None))
    if not handler or not required_role:
        return await respond_ephemeral_embed("Unsupported action", "This button is not configured.")

    member = payload.get("member", {})
    user_roles = {int(r) for r in member.get("roles", [])}
    if required_role not in user_roles:
        return await respond_ephemeral_embed("Permissions Denied", "You do not have the required role to perform this action.")

    moderator_id = member.get("user", {}).get("id")
    moderator_username = member.get("user", {}).get("username", "N/A")
    original_embed = (payload["message"].get("embeds") or [{}])[0]

    try:
        final_embed, error, meta = await handler(parts, moderator_id, moderator_username, original_embed, payload)
        if error:
            logging.error(f"Handler for '{action}' failed: {error}")
            # On error, we can send an ephemeral message to the moderator
            return await respond_ephemeral_embed("Action Failed", error)

        # For initial accept, the message is deleted, so we don't update it
        if action == "roblox_initial_accept":
             return await respond_ephemeral_embed("Success", "Appeal has been forwarded for final review.")
        if action == "roblox_initial_decline":
             return await respond_ephemeral_embed("Success", "Appeal has been declined.")

        # If handler requested deletion (final review), delete original message and confirm ephemerally
        if meta and meta.get("delete_message"):
            await delete_message(payload["channel_id"], payload["message"]["id"])
            if meta.get("log_channel") and meta.get("log_embed"):
                await post_channel_message(meta["log_channel"], embed=meta["log_embed"])
            return await respond_ephemeral_embed("Success", meta.get("ephemeral", "Action completed."))

        if meta and meta.get("log_channel") and meta.get("log_embed"):
            await post_channel_message(meta["log_channel"], embed=meta["log_embed"])

        return JSONResponse({
            "type": 7, # UPDATE_MESSAGE
            "data": {
                "embeds": [final_embed],
                "components": [] # remove buttons
            }
        })
    except Exception as e:
        logging.exception(f"Error processing action '{action}': {e}")
        return await respond_ephemeral_embed("Error", "An unexpected server error occurred.")
