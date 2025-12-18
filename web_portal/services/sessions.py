from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.responses import Response

from ..settings import PERSIST_SESSION_SECONDS, SECRET_KEY, SESSION_COOKIE_NAME
from ..utils import clean_display_name
from .discord_api import (
    fetch_discord_user,
    get_valid_access_token as get_valid_discord_token,
)
from .roblox_api import (
    get_user_info as get_roblox_user_info,
    get_valid_access_token as get_valid_roblox_token,
)
from ..state import _session_epoch
from .supabase import get_portal_flag_sync

serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")


def persist_session(
    response: Response,
    internal_user_id: str,
    platform_type: str,  # "discord" or "roblox"
    platform_id: str,
    username: str,
    display_name: str,
) -> dict:
    """
    Persists a single-platform user session.
    A new session is created, discarding any previous multi-platform session data.
    """
    session = {
        "internal_user_id": internal_user_id,
        "logged_in_platform": platform_type,
        "iat": time.time(),
        "display_name": display_name,
    }

    if platform_type == "discord":
        session["uid"] = platform_id
        session["uname"] = username
    elif platform_type == "roblox":
        session["ruid"] = platform_id
        session["runame"] = username
    else:
        logging.error("Invalid platform_type passed to persist_session: %s", platform_type)
        raise ValueError("Invalid platform_type")

    session["epoch"] = _session_epoch
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


def update_session_with_platform(
    response: Response,
    existing_session: dict,
    platform_type: str,
    platform_id: str,
    username: str,
    display_name: str,
    *,
    internal_user_id: Optional[str] = None,
) -> dict:
    """
    Updates an existing session with a new platform's data.
    """
    updated_session = dict(existing_session)

    if platform_type == "discord":
        updated_session["uid"] = platform_id
        updated_session["uname"] = username
    elif platform_type == "roblox":
        updated_session["ruid"] = platform_id
        updated_session["runame"] = username
    else:
        logging.error("Invalid platform_type passed to update_session_with_platform: %s", platform_type)
        raise ValueError("Invalid platform_type")

    if internal_user_id:
        updated_session["internal_user_id"] = internal_user_id

    if display_name and len(display_name) > len(updated_session.get("display_name", "")):
        updated_session["display_name"] = display_name

    updated_session["iat"] = time.time()
    updated_session["epoch"] = _session_epoch
    token = serializer.dumps(updated_session)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=PERSIST_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return updated_session


def maybe_persist_session(request: Request, response: Response, session: Optional[dict], refreshed: bool) -> None:
    if not session:
        return

    session["iat"] = time.time()
    session["epoch"] = _session_epoch
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
        remote_epoch = get_portal_flag_sync("session_epoch", _session_epoch)
        if data.get("epoch") is not None and data.get("epoch") != remote_epoch:
            logging.info("Session epoch mismatch; forcing logout.")
            return None
        return data
    except BadSignature:
        logging.warning("Invalid session cookie signature. Session potentially tampered with or corrupt.")
        return None


async def refresh_session_profile(session: Optional[dict]) -> Tuple[Optional[dict], bool]:
    if not session:
        return None, False

    updated = dict(session)
    refreshed = False

    logged_in_platform = updated.get("logged_in_platform")
    if not logged_in_platform:
        logging.debug("Session has no logged_in_platform, skipping profile refresh.")
        return session, False

    if logged_in_platform == "discord" and "uid" in updated:
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

    elif logged_in_platform == "roblox" and "ruid" in updated:
        user_id = updated["ruid"]
        token = await get_valid_roblox_token(str(user_id))
        if token:
            try:
                user = await get_roblox_user_info(token)
                uname_label = user.get("name") or user.get("preferred_username")
                display_name = clean_display_name(user.get("nickname") or uname_label)
                updated["runame"] = uname_label
                updated["display_name"] = display_name
                refreshed = True
            except Exception as exc:
                logging.debug("Roblox profile refresh failed for %s: %s", user_id, exc)

    if refreshed:
        updated["iat"] = time.time()
        return updated, True
    return session, False
