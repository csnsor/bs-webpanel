import time
import secrets
import logging
from typing import Optional, Tuple, Dict
import httpx
from fastapi import Request, HTTPException
from config import (
    DISCORD_API_BASE, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, 
    DISCORD_REDIRECT_URI, OAUTH_SCOPES
)
from app.models import DiscordUser
from app.utils import get_client_ip

# In-memory stores for OAuth state and user tokens
_state_tokens: Dict[str, Tuple[str, float]] = {}  # {token: (ip, issued_at)}
_user_tokens: Dict[str, Dict[str, Any]] = {}  # {user_id: {"access_token": str, "refresh_token": str, "expires_at": float}}

def oauth_authorize_url(state: str) -> str:
    return (
        f"{DISCORD_API_BASE}/oauth2/authorize"
        f"?response_type=code&client_id={DISCORD_CLIENT_ID}"
        f"&scope={OAUTH_SCOPES}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&state={state}"
        f"&prompt=none"
    )

def issue_state_token(ip: str) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    _state_tokens[token] = (ip, now)
    # prune stale tokens (>15 minutes)
    for t, (_, ts) in list(_state_tokens.items()):
        if now - ts > 900:
            _state_tokens.pop(t, None)
    return token

def validate_state_token(token: str, ip: str) -> bool:
    if not token:
        return False
    record = _state_tokens.pop(token, None)
    if not record:
        return False
    saved_ip, ts = record
    if time.time() - ts > 900:
        return False
    if ip in {"unknown", "", None} or saved_ip in {"unknown", "", None}:
        return False
    if saved_ip != ip:
        return False
    return True

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

def store_user_token(user_id: str, token_data: dict):
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

async def fetch_discord_user(access_token: str) -> DiscordUser:
    client = get_http_client()
    resp = await client.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return DiscordUser(**resp.json())

async def refresh_session_profile(session: Optional[dict]) -> Tuple[Optional[dict], bool]:
    if not session:
        return None, False
    user_id = session.get("uid")
    if not user_id:
        return session, False
    token = await get_valid_access_token(str(user_id))
    if not token:
        return session, False
    try:
        user = await fetch_discord_user(token)
    except Exception as exc:
        logging.debug("Profile refresh failed for %s: %s", user_id, exc)
        return session, False
    uname_label = f"{user.username}#{user.discriminator}"
    display_name = clean_display_name(user.global_name or user.username or uname_label)
    updated = dict(session)
    updated["uname"] = uname_label
    updated["display_name"] = display_name
    updated["iat"] = time.time()
    return updated, True