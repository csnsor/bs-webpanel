from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

import httpx

from ..clients import get_http_client
from ..settings import (
    ROBLOX_SUPABASE_TABLE,
    SUPABASE_KEY,
    SUPABASE_SESSION_TABLE,
    SUPABASE_TABLE,
    SUPABASE_URL,
    TARGET_GUILD_ID,
)


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

def _canonical_internal_id(discord_id: Optional[str], roblox_id: Optional[str]) -> str:
    if discord_id and roblox_id:
        digest = hashlib.sha256(f"{discord_id}:{roblox_id}".encode("utf-8")).hexdigest()
        return f"link:{digest}"
    if discord_id:
        return f"discord:{discord_id}"
    if roblox_id:
        return f"roblox:{roblox_id}"
    return f"anon:{uuid.uuid4().hex}"


async def _query_internal_id(table: str, column: str, value: str) -> Optional[str]:
    if not (value and is_supabase_ready()):
        return None
    records = await supabase_request(
        "get",
        table,
        params={column: f"eq.{value}", "limit": 1, "select": "internal_user_id"},
    )
    if records and isinstance(records, list):
        return records[0].get("internal_user_id")
    return None


async def _patch_internal_id(table: str, column: str, value: str, payload: dict) -> None:
    if not (value and is_supabase_ready()):
        return
    try:
        await supabase_request(
            "patch",
            table,
            params={column: f"eq.{value}"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=representation",
        )
    except Exception:
        # supabase_request already logs failures.
        pass


async def resolve_internal_user_id(
    *,
    discord_id: Optional[str] = None,
    roblox_id: Optional[str] = None,
    current_id: Optional[str] = None,
) -> str:
    """
    Determine the canonical internal user ID for the provided platform IDs.
    Ensures existing appeal rows are normalized to the chosen ID.
    """
    if current_id:
        target_id = current_id
    else:
        discord_internal = await _query_internal_id(SUPABASE_TABLE, "user_id", discord_id) if discord_id else None
        roblox_internal = await _query_internal_id(ROBLOX_SUPABASE_TABLE, "roblox_id", roblox_id) if roblox_id else None

        if discord_internal and roblox_internal:
            target_id = discord_internal if discord_internal == roblox_internal else discord_internal
        elif discord_internal or roblox_internal:
            target_id = discord_internal or roblox_internal
        else:
            target_id = _canonical_internal_id(discord_id, roblox_id)

    if not is_supabase_ready():
        return target_id

    if discord_id:
        await _patch_internal_id(
            SUPABASE_TABLE,
            "user_id",
            discord_id,
            {"internal_user_id": target_id},
        )
    if roblox_id:
        payload: Dict[str, Any] = {"internal_user_id": target_id}
        if discord_id:
            payload["discord_user_id"] = discord_id
        await _patch_internal_id(
            ROBLOX_SUPABASE_TABLE,
            "roblox_id",
            roblox_id,
            payload=payload,
        )

    return target_id


async def log_appeal_to_supabase(
    appeal_id: str,
    user: Dict[str, Any],
    internal_user_id: str,
    ban_reason: str,
    ban_evidence: str,
    appeal_reason: str,
    appeal_reason_original: str,
    user_lang: str,
    message_cache: Optional[List[dict]],
    ip: str,
    forwarded_for: str,
    user_agent: str,
) -> None:
    payload = {
        "appeal_id": appeal_id,
        "user_id": user["id"],
        "internal_user_id": internal_user_id,
        "username": user.get("username"),
        "guild_id": TARGET_GUILD_ID,
        "ban_reason": ban_reason,
        "ban_evidence": ban_evidence,
        "appeal_reason": appeal_reason,
        "appeal_reason_original": appeal_reason_original,
        "user_lang": user_lang,
        "status": "pending",
        "ip": ip,
        "forwarded_for": forwarded_for,
        "user_agent": user_agent,
        "message_cache": message_cache,
    }
    await supabase_request("post", SUPABASE_TABLE, payload=payload, prefer="return=minimal")


async def fetch_appeal_history(internal_user_id: str, limit: int = 25, *, select: Optional[str] = None) -> List[dict]:
    params = {
        "internal_user_id": f"eq.{internal_user_id}",
        "order": "created_at.desc",
        "limit": min(limit, 100),
    }
    if select:
        params["select"] = select
    else:
        params["select"] = "appeal_id,status,created_at,ban_reason,appeal_reason"
    records = await supabase_request("get", SUPABASE_TABLE, params=params)
    return records or []


async def fetch_appeal_record(appeal_id: str) -> Optional[dict]:
    records = await supabase_request(
        "get",
        SUPABASE_TABLE,
        params={"appeal_id": f"eq.{appeal_id}", "limit": 1},
    )
    if records:
        return records[0]
    return None


async def get_remote_last_submit(user_id: str) -> Optional[float]:
    recs = await supabase_request(
        "get",
        SUPABASE_SESSION_TABLE,
        params={"user_id": f"eq.{user_id}", "order": "last_submit.desc", "limit": 1, "select": "last_submit"},
    )
    if recs:
        try:
            return float(recs[0].get("last_submit") or 0)
        except Exception:
            return None
    return None


async def is_session_token_used(token_hash: str) -> bool:
    recs = await supabase_request(
        "get",
        SUPABASE_SESSION_TABLE,
        params={"token_hash": f"eq.{token_hash}", "limit": 1, "select": "token_hash"},
    )
    return bool(recs)


async def mark_session_token(token_hash: str, user_id: str, ts: float) -> None:
    payload = {"token_hash": token_hash, "user_id": user_id, "last_submit": int(ts)}
    await supabase_request(
        "post",
        SUPABASE_SESSION_TABLE,
        payload=payload,
        prefer="resolution=merge-duplicates,return=minimal",
    )


async def update_appeal_status(
    appeal_id: str,
    status: str,
    moderator_id: Optional[str],
    dm_delivered: bool,
    notes: Optional[str] = None,
) -> None:
    payload = {
        "status": status,
        "decision_by": moderator_id,
        "decision_at": int(time.time()),
        "dm_delivered": dm_delivered,
        "notes": notes,
    }
    await supabase_request("patch", SUPABASE_TABLE, params={"appeal_id": f"eq.{appeal_id}"}, payload=payload, prefer="return=minimal")
