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
from ..services.discord_api import add_user_to_guild, dm_user, maybe_remove_from_dm_guild, remove_from_target_guild
from ..services.interactions import (
    build_decision_embed,
    delete_message,
    respond_ephemeral_embed,
    update_message,
    verify_signature,
)
from ..services.supabase import fetch_appeal_record, update_appeal_status
from ..settings import (
    APPEAL_LOG_CHANNEL_ID,
    DISCORD_API_BASE,
    DISCORD_BOT_TOKEN,
    INVITE_LINK,
    MODERATOR_ROLE_ID,
    READD_GUILD_ID,
    TARGET_GUILD_ID,
)
from ..state import _appeal_locked, _declined_users, _processed_appeals
from ..utils import normalize_language

router = APIRouter()


@router.post("/interactions")
async def interactions(request: Request):
    body = await request.body()
    logging.info("Interactions: received request, bytes=%s", len(body))
    if not verify_signature(request, body):
        logging.warning("Interactions: invalid signature")
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    payload = await request.json()
    logging.info("Interactions: payload type=%s id=%s", payload.get("type"), payload.get("id"))
    if payload["type"] == 1:  # PING
        return JSONResponse({"type": 1})

    if payload["type"] == 3:  # COMPONENT
        data = payload.get("data", {})
        custom_id = data.get("custom_id", "")
        member = payload.get("member") or {}
        user_obj = member.get("user") or {}
        moderator_id = user_obj.get("id")
        logging.info(
            "Interactions: component custom_id=%s moderator=%s channel=%s message=%s",
            custom_id,
            moderator_id,
            payload.get("channel_id"),
            payload.get("message", {}).get("id"),
        )

        now = time.time()
        for k in list(_processed_appeals.keys()):
            if now - _processed_appeals[k] > 3600:
                _processed_appeals.pop(k, None)

        roles = set(map(int, member.get("roles", [])))
        if MODERATOR_ROLE_ID not in roles:
            return await respond_ephemeral_embed(
                "Not allowed",
                "You don't have the moderator role required to handle appeals.",
                0xE74C3C,
            )

        try:
            action, appeal_id, user_id = custom_id.split(":")
        except ValueError:
            logging.warning("Interactions: malformed custom_id=%s", custom_id)
            return await respond_ephemeral_embed("Invalid request", "Bad interaction payload.")

        channel_id = payload["channel_id"]
        message_id = payload["message"]["id"]
        embeds = payload["message"].get("embeds") or []
        original_embed = copy.deepcopy(embeds[0]) if embeds else {}

        if not appeal_id or not user_id or action not in {"web_appeal_accept", "web_appeal_decline"}:
            return await respond_ephemeral_embed("Invalid request", "Malformed interaction data.")

        def updated_embed(status: str, note: Optional[str] = None) -> dict:
            embed = copy.deepcopy(original_embed) or {}
            if status == "accepted":
                color = 0x2ECC71
                suffix = " (ACCEPTED)"
                label = "Accepted"
            else:
                color = 0xE74C3C
                suffix = " (DECLINED)"
                label = "Declined"
            embed["color"] = color
            embed["title"] = embed.get("title", "Appeal") + suffix
            embed["fields"] = embed.get("fields", []) + [
                {"name": "Action Taken", "value": f"{label} by <@{moderator_id}>", "inline": False}
            ]
            if note:
                embed["fields"].append({"name": "Notes", "value": note, "inline": False})
            return embed

        async def handle_accept() -> Tuple[Optional[dict], Optional[str]]:
            if appeal_id in _processed_appeals:
                return None, "Appeal already processed."
            _processed_appeals[appeal_id] = time.time()
            try:
                appeal_record = await fetch_appeal_record(appeal_id)
                user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))

                client = get_http_client()
                unban_resp = await client.delete(
                    f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                )
                unban_status = unban_resp.status_code
                if unban_status not in (200, 204, 404):
                    raise RuntimeError(f"Unban failed with status {unban_status}")

                removal_status = await remove_from_target_guild(user_id)
                readd_status = await add_user_to_guild(user_id, READD_GUILD_ID)

                accept_desc_en = "Your appeal has been reviewed and accepted. You have been unbanned and re-added to the server."
                accept_desc = (
                    await translate_text(accept_desc_en, target_lang=user_lang, source_lang="en")
                    if user_lang != "en"
                    else accept_desc_en
                )
                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Accepted",
                        "description": accept_desc,
                        "color": 0x2ECC71,
                    },
                )

                await update_appeal_status(
                    appeal_id=appeal_id,
                    status="accepted",
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    notes=f"unban:{unban_status} removal:{removal_status}",
                )

                log_embed = build_decision_embed(
                    status="accepted",
                    appeal_id=appeal_id,
                    user_id=user_id,
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    invite_link=INVITE_LINK,
                    unban_status=unban_status,
                    removal_status=removal_status,
                    add_status=readd_status,
                )

                await client.post(
                    f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={"embeds": [log_embed]},
                )

                note = []
                if unban_status not in (200, 204):
                    note.append(f"Unban status: {unban_status}")
                if removal_status and removal_status not in (200, 204, 404):
                    note.append(f"Removal status: {removal_status}")
                if readd_status and readd_status not in (200, 201, 204):
                    note.append(f"Re-add status: {readd_status}")
                if not dm_delivered:
                    note.append("DM delivery failed")
                note_text = "; ".join(note) if note else None
                return updated_embed("accepted", note_text), None
            except Exception as exc:
                _processed_appeals.pop(appeal_id, None)
                logging.exception("Failed to process acceptance for appeal %s: %s", appeal_id, exc)
                return None, "Unable to accept appeal. Check bot permissions and try again."

        async def handle_decline() -> Tuple[Optional[dict], Optional[str]]:
            if appeal_id in _processed_appeals:
                return None, "Appeal already processed."
            _processed_appeals[appeal_id] = time.time()
            try:
                _declined_users[user_id] = True
                _appeal_locked[user_id] = True

                appeal_record = await fetch_appeal_record(appeal_id)
                user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))

                removal_status = await remove_from_target_guild(user_id)

                decline_desc_en = (
                    "Your appeal has been reviewed and declined. Further appeals are blocked for this ban.\n"
                    "You have been removed from the guild for security."
                )
                decline_desc = (
                    await translate_text(decline_desc_en, target_lang=user_lang, source_lang="en")
                    if user_lang != "en"
                    else decline_desc_en
                )
                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Declined",
                        "description": decline_desc,
                        "color": 0xE74C3C,
                    },
                )

                await update_appeal_status(
                    appeal_id=appeal_id,
                    status="declined",
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    notes=f"declined removal:{removal_status}",
                )

                log_embed = build_decision_embed(
                    status="declined",
                    appeal_id=appeal_id,
                    user_id=user_id,
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    removal_status=removal_status,
                )

                client = get_http_client()
                await client.post(
                    f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={"embeds": [log_embed]},
                )

                await maybe_remove_from_dm_guild(user_id)

                note = []
                if removal_status and removal_status not in (200, 204, 404):
                    note.append(f"Removal status: {removal_status}")
                if not dm_delivered:
                    note.append("DM delivery failed")
                return updated_embed("declined", "; ".join(note) if note else None), None
            except Exception as exc:
                _processed_appeals.pop(appeal_id, None)
                logging.exception("Failed to process decline for appeal %s: %s", appeal_id, exc)
                return None, "Unable to decline appeal right now."

        async def process_and_cleanup():
            try:
                processing = copy.deepcopy(original_embed) or {}
                processing["color"] = 0xF59E0B
                processing["title"] = (processing.get("title") or "Appeal") + " (PROCESSING)"
                processing["fields"] = (processing.get("fields") or []) + [
                    {"name": "Action", "value": f"Processing by <@{moderator_id}> [{moderator_id}]", "inline": False}
                ]
                await update_message(channel_id, message_id, embeds=[processing], components=[])

                if action == "web_appeal_accept":
                    embed, error = await handle_accept()
                else:
                    embed, error = await handle_decline()

                if error:
                    fail = copy.deepcopy(original_embed) or {}
                    fail["color"] = 0xE74C3C
                    fail["title"] = (fail.get("title") or "Appeal") + " (FAILED)"
                    fail["fields"] = (fail.get("fields") or []) + [{"name": "Error", "value": error, "inline": False}]
                    await update_message(channel_id, message_id, embeds=[fail], components=[])
                    return

                deleted = await delete_message(channel_id, message_id)
                if deleted not in (200, 204, 404):
                    await update_message(channel_id, message_id, embeds=[embed], components=[])
                logging.info(
                    "Interactions: processed action=%s appeal=%s user=%s [%s] delete_status=%s",
                    action,
                    appeal_id,
                    user_id,
                    user_id,
                    deleted,
                )
            except Exception as exc:
                logging.exception("Interactions background task failed: %s", exc)

        if action in {"web_appeal_accept", "web_appeal_decline"}:
            asyncio.create_task(process_and_cleanup())
            return JSONResponse({"type": 6})

    return JSONResponse({"type": 4, "data": {"content": "Unsupported interaction", "flags": 1 << 6}})

