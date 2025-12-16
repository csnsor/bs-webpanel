from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.responses import Response

from ..settings import PERSIST_SESSION_SECONDS, SECRET_KEY, SESSION_COOKIE_NAME
from ..utils import clean_display_name
from .discord_api import fetch_discord_user, get_valid_access_token as get_valid_discord_token
from .roblox_api import get_user_info as get_roblox_user_info, get_valid_access_token as get_valid_roblox_token

serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")


def persist_session(
    request: Request,
    response: Response,
    discord_user_id: Optional[str] = None,
    discord_username: Optional[str] = None,
    discord_display_name: Optional[str] = None,
    roblox_user_id: Optional[str] = None,
    roblox_username: Optional[str] = None,
    roblox_display_name: Optional[str] = None,
) -> dict:
    session = read_user_session(request) or {}
    
    if discord_user_id:
        session["uid"] = discord_user_id
        session["uname"] = discord_username
    
    if roblox_user_id:
        session["ruid"] = roblox_user_id
        session["runame"] = roblox_username
    
    # Handle display_name logic:
    # 1. If a discord_display_name is provided, use it.
    # 2. Else if a roblox_display_name is provided, use it.
    # 3. Else, if the session already has a display_name, keep it.
    # 4. Otherwise, no display_name is set.
    if discord_display_name:
        session["display_name"] = discord_display_name
    elif roblox_display_name and "display_name" not in session: # Only set if Discord didn't provide one
        session["display_name"] = roblox_display_name

    session["iat"] = time.time()
    
    token = serializer.dumps(session)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=PERSIST_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return session


def maybe_persist_session(request: Request, response: Response, session: Optional[dict], refreshed: bool) -> None:
    if session and refreshed:
        token = serializer.dumps(session)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=PERSIST_SESSION_SECONDS,
            secure=True,
            httponly=True,
            samesite="Lax",
        )



def read_user_session(request: Request) -> Optional[dict]:
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    try:
        data = serializer.loads(raw)
        if time.time() - float(data.get("iat", 0)) > PERSIST_SESSION_SECONDS * 2:
            return None
        return data
    except BadSignature:
        return None


async def refresh_session_profile(session: Optional[dict]) -> Tuple[Optional[dict], bool]:
    if not session:
        return None, False

    updated = dict(session)
    refreshed = False

    # Handle Discord session refresh
    if "uid" in updated:
        user_id = updated["uid"]
        token = await get_valid_discord_token(str(user_id))
        if token:
            try:
                user = await fetch_discord_user(token)
                uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
                display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
                updated["uname"] = uname_label
                updated["display_name"] = display_name
                refreshed = True
            except Exception as exc:
                logging.debug("Discord profile refresh failed for %s: %s", user_id, exc)

    # Handle Roblox session refresh
    if "ruid" in updated:
        user_id = updated["ruid"]
        token = await get_valid_roblox_token(str(user_id))
        if token:
            try:
                user = await get_roblox_user_info(token)
                uname_label = user.get("name") or user.get("preferred_username")
                display_name = clean_display_name(user.get("nickname") or uname_label)
                updated["runame"] = uname_label
                # Give preference to Discord display name if available
                if "display_name" not in updated:
                    updated["display_name"] = display_name
                refreshed = True
            except Exception as exc:
                logging.debug("Roblox profile refresh failed for %s: %s", user_id, exc)
    
    if refreshed:
        updated["iat"] = time.time()
        return updated, True

    return session, False

