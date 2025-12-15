from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from ..clients import get_http_client
from ..settings import DISCORD_API_BASE, DISCORD_BOT_TOKEN, DISCORD_PUBLIC_KEY


def verify_signature(request: Request, body: bytes) -> bool:
    try:
        import nacl.exceptions  # type: ignore
        import nacl.signing  # type: ignore
    except Exception:
        logging.error('PyNaCl is required for Discord signature verification. Install "PyNaCl".')
        return False

    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    if not signature or not timestamp:
        return False
    try:
        key = nacl.signing.VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        key.verify(f"{timestamp}".encode() + body, bytes.fromhex(signature))
        return True
    except (ValueError, nacl.exceptions.BadSignatureError):
        return False


async def respond_ephemeral(content: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": 4,
            "data": {"content": content, "flags": 1 << 6},
        }
    )


async def respond_ephemeral_embed(title: str, description: str, color: int = 0xE67E22) -> JSONResponse:
    return JSONResponse(
        {
            "type": 4,
            "data": {
                "flags": 1 << 6,
                "embeds": [{"title": title, "description": description, "color": color}],
            },
        }
    )


async def delete_message(channel_id: str, message_id: str) -> Optional[int]:
    client = get_http_client()
    resp = await client.delete(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )
    return resp.status_code


async def update_message(channel_id: str, message_id: str, *, embeds: List[dict], components: Optional[list] = None) -> Optional[int]:
    client = get_http_client()
    payload: dict = {"embeds": embeds}
    if components is not None:
        payload["components"] = components
    resp = await client.patch(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json=payload,
    )
    return resp.status_code


def build_decision_embed(
    status: str,
    appeal_id: str,
    user_id: str,
    moderator_id: str,
    dm_delivered: bool,
    *,
    invite_link: Optional[str] = None,
    unban_status: Optional[int] = None,
    removal_status: Optional[int] = None,
    add_status: Optional[int] = None,
):
    accepted = status == "accepted"
    color = 0x2ECC71 if accepted else 0xE74C3C
    title = "Appeal Accepted" if accepted else "Appeal Declined"
    desc = f"Appeal `{appeal_id}` {title.lower()}.\nUser: <@{user_id}> [{user_id}]\nModerator: <@{moderator_id}>"
    if invite_link and accepted:
        desc += f"\nInvite: {invite_link}"
    fields = [
        {"name": "DM delivery", "value": "Delivered" if dm_delivered else "Failed", "inline": True},
    ]
    if unban_status:
        fields.append({"name": "Unban", "value": str(unban_status), "inline": True})
    if removal_status:
        fields.append({"name": "Guild removal", "value": str(removal_status), "inline": True})
    if add_status:
        fields.append({"name": "Guild add", "value": str(add_status), "inline": True})
    return {
        "title": title,
        "description": desc,
        "color": color,
        "fields": fields,
    }

