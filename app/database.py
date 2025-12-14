import logging
import time
from typing import Optional, Dict, List, Any
import httpx
from config import SUPABASE_URL, SUPABASE_KEY, SUPABASE_TABLE, SUPABASE_SESSION_TABLE, SUPABASE_CONTEXT_TABLE
from app.models import AppealRecord, MessageContext
from app.utils import get_http_client

def is_supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)

async def supabase_request(method: str, table: str, *, params: Optional[dict] = None, payload: Optional[dict] = None, prefer: Optional[str] = None):
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
        if resp.content:
            return resp.json()
    except Exception as exc:
        logging.warning("Supabase request failed table=%s method=%s error=%s", table, method, exc)
    return None

async def log_appeal_to_supabase(
    appeal_id: str,
    user: dict,
    ban_reason: str,
    ban_evidence: str,
    appeal_reason: str,
    appeal_reason_original: str,
    user_lang: str,
    message_cache: Optional[List[dict]],
    ip: str,
    forwarded_for: str,
    user_agent: str,
):
    payload = {
        "appeal_id": appeal_id,
        "user_id": user["id"],
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
    await supabase_request("post", SUPABASE_TABLE, payload=payload)

async def get_remote_last_submit(user_id: str) -> Optional[float]:
    recs = await supabase_request(
        "get",
        SUPABASE_SESSION_TABLE,
        params={"user_id": f"eq.{user_id}", "order": "last_submit.desc", "limit": 1},
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
        params={"token_hash": f"eq.{token_hash}", "limit": 1},
    )
    return bool(recs)

async def mark_session_token(token_hash: str, user_id: str, ts: float):
    payload = {"token_hash": token_hash, "user_id": user_id, "last_submit": int(ts)}
    await supabase_request(
        "post",
        SUPABASE_SESSION_TABLE,
        payload=payload,
        prefer="resolution=merge-duplicates",
    )

async def update_appeal_status(
    appeal_id: str,
    status: str,
    moderator_id: Optional[str],
    dm_delivered: bool,
    notes: Optional[str] = None,
):
    payload = {
        "status": status,
        "decision_by": moderator_id,
        "decision_at": int(time.time()),
        "dm_delivered": dm_delivered,
        "notes": notes,
    }
    await supabase_request("patch", SUPABASE_TABLE, params={"appeal_id": f"eq.{appeal_id}"}, payload=payload)

async def fetch_appeal_history(user_id: str, limit: int = 25) -> List[dict]:
    params = {"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": min(limit, 100)}
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

async def persist_message_snapshot(user_id: str, messages: List[dict]):
    if not is_supabase_ready() or not messages:
        return
    logging.info("Persisting %d messages for user %s", len(messages[-15:]), user_id)
    try:
        await supabase_request(
            "post",
            "user_message_snapshots",
            params={"on_conflict": "user_id"},
            payload={"user_id": user_id, "messages": messages[-15:]},
            prefer="resolution=merge-duplicates",
        )
    except Exception as exc:
        logging.warning("Snapshot persist failed for %s: %s", user_id, exc)

async def fetch_message_cache(user_id: str, limit: int = 15) -> List[dict]:
    """Fetch ban context from the permanent ban log in Supabase."""
    if not is_supabase_ready():
        return _get_recent_message_context(user_id, limit)
    try:
        recs = await supabase_request(
            "get",
            "banned_user_context",
            params={"user_id": f"eq.{user_id}", "limit": 1},
        )
        if recs and recs[0].get("messages"):
            messages = recs[0]["messages"]

            def get_ts(m: dict) -> float:
                t = m.get("timestamp", 0)
                try:
                    return float(t)
                except Exception:
                    return 0.0

            return sorted(messages, key=get_ts, reverse=True)[:limit]
    except Exception as exc:
        logging.warning("Failed to fetch context for %s: %s", user_id, exc)
    return _get_recent_message_context(user_id, limit)