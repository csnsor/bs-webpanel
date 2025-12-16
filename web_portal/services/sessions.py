from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.responses import Response

from ..settings import PERSIST_SESSION_SECONDS, SECRET_KEY, SESSION_COOKIE_NAME
from ..utils import clean_display_name
from .discord_api import fetch_discord_user, get_valid_access_token

serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")


def persist_user_session(request: Request, response: Response, user_id: str, username: str, display_name: Optional[str] = None) -> dict:
    session = read_user_session(request) or {}
    session.update({
        "type": "discord",
        "uid": user_id,
        "uname": username,
        "iat": time.time(),
        "display_name": display_name or username
    })
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

def persist_roblox_user_session(request: Request, response: Response, user_id: str, username: str, display_name: Optional[str] = None) -> dict:
    session = read_user_session(request) or {}
    session.update({
        "type": "roblox",
        "ruid": user_id,
        "runame": username,
        "iat": time.time(),
        "display_name": display_name or username
    })
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
        if session.get("type") == "discord":
            persist_user_session(
                request,
                response,
                session["uid"],
                session.get("uname") or "",
                display_name=session.get("display_name"),
            )
        elif session.get("type") == "roblox":
             persist_roblox_user_session(
                request,
                response,
                session["ruid"],
                session.get("runame") or "",
                display_name=session.get("display_name"),
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

    # Handle Discord session refresh
    if session.get("type") == "discord":
        user_id = session.get("uid")
        if not user_id:
            return session, False
        token = await get_valid_access_token(str(user_id))
        if not token:
            return session, False
        try:
            user = await fetch_discord_user(token)
            uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
            display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
            updated = dict(session)
            updated["uname"] = uname_label
            updated["display_name"] = display_name
            updated["iat"] = time.time()
            return updated, True
        except Exception as exc:
            logging.debug("Discord profile refresh failed for %s: %s", user_id, exc)
            return session, False

    # Placeholder for Roblox session refresh
    if session.get("type") == "roblox":
        # Roblox token refresh logic would go here if implemented
        return session, False

    return session, False

