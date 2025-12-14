import time
import uuid
import asyncio
import logging
from typing import Dict, List, Optional
from fastapi import Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from config import (
    APPEAL_COOLDOWN_SECONDS, APPEAL_IP_MAX_REQUESTS, APPEAL_IP_WINDOW_SECONDS,
    APPEAL_WINDOW_SECONDS, TARGET_GUILD_ID, READD_GUILD_ID, INVITE_LINK,
    MODERATOR_ROLE_ID, APPEAL_CHANNEL_ID, APPEAL_LOG_CHANNEL_ID,
    LIBRETRANSLATE_URL
)
from app.models import AppealSubmission, AppealRecord
from app.utils import (
    uid, normalize_language, format_timestamp, hash_ip, get_client_ip,
    wants_html, read_user_session, persist_user_session, maybe_persist_session,
    refresh_session_profile, build_user_chip, LANG_STRINGS
)
from app.database import (
    log_appeal_to_supabase, get_remote_last_submit, is_session_token_used,
    mark_session_token, update_appeal_status, fetch_appeal_history,
    fetch_appeal_record, fetch_message_cache
)
from app.bot import (
    fetch_ban_if_exists, ensure_dm_guild_membership, maybe_remove_from_dm_guild,
    remove_from_target_guild, add_user_to_guild, send_log_message
)
from app.auth import exchange_code_for_token, store_user_token, get_valid_access_token, fetch_discord_user
from app.templates import render_page, render_error, render_history_items

# In-memory stores for rate limiting and appeal tracking
_appeal_rate_limit: Dict[str, float] = {}  # {user_id: timestamp_of_last_submit}
_used_sessions: Dict[str, float] = {}  # {session_token: timestamp_used}
_ip_requests: Dict[str, List[float]] = {}  # {ip: [timestamps]}
_ban_first_seen: Dict[str, float] = {}  # {user_id: first time we saw the ban}
_appeal_locked: Dict[str, bool] = {}  # {user_id: True if appealed already}
_processed_appeals: Dict[str, float] = {}  # {appeal_id: timestamp_processed}
_declined_users: Dict[str, bool] = {}  # {user_id: True if appeal declined}

def enforce_ip_rate_limit(ip: str):
    now = time.time()
    window_start = now - APPEAL_IP_WINDOW_SECONDS
    if len(_ip_requests) > 10000:
        _ip_requests.clear()
    bucket = _ip_requests.setdefault(ip, [])
    bucket = [t for t in bucket if t >= window_start]
    if len(bucket) >= APPEAL_IP_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down and try again.")
    bucket.append(now)
    _ip_requests[ip] = bucket

async def detect_language(request: Request, lang_param: Optional[str] = None) -> str:
    if lang_param:
        return normalize_language(lang_param)
    cookie_lang = request.cookies.get("lang")
    if cookie_lang:
        return normalize_language(cookie_lang)
    accept = request.headers.get("accept-language", "")
    if accept:
        return normalize_language(accept.split(",")[0].strip())
    ip = get_client_ip(request)
    if ip and ip not in {"127.0.0.1", "::1", "unknown"}:
        try:
            client = get_http_client()
            resp = await client.get(f"https://ipapi.co/{ip}/json/", timeout=3)
            if resp.status_code == 200:
                data = resp.json() or {}
                langs = data.get("languages")
                if langs:
                    return normalize_language(langs.split(",")[0])
                cc = data.get("country_code")
                if cc:
                    return normalize_language(cc.lower())
        except Exception as exc:
            logging.warning("Geo lookup failed for ip=%s error=%s", ip, exc)
    return "en"

async def get_strings(lang: str) -> Dict[str, str]:
    lang = normalize_language(lang)
    base = LANG_STRINGS["en"]
    if lang in LANG_STRINGS:
        return LANG_STRINGS[lang]
    if lang in LANG_CACHE:
        return LANG_CACHE[lang]
    translated: Dict[str, str] = {}
    for key, text in base.items():
        translated[key] = await translate_text(text, target_lang=lang, source_lang="en")
    merged = {**base, **translated}
    LANG_CACHE[lang] = merged
    return merged

async def translate_text(text: str, target_lang: str = "en", source_lang: Optional[str] = None) -> str:
    if not text or normalize_language(target_lang) == "en" and normalize_language(source_lang) == "en":
        return text
    try:
        client = get_http_client()
        resp = await client.post(
            LIBRETRANSLATE_URL,
            json={
                "q": text,
                "source": source_lang or "auto",
                "target": target_lang,
                "format": "text",
            },
            headers={"Content-Type": "application/json"},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("translatedText") or text
        logging.warning("Translation failed status=%s body=%s", resp.status_code, resp.text)
    except Exception as exc:
        logging.warning("Translation exception: %s", exc)
    return text

async def handle_appeal_submission(
    request: Request,
    session: dict,
    user: dict,
    ban_data: dict,
    appeal_reason: str,
    user_lang: str,
    strings: dict,
):
    user_id = uid(user["id"])
    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    user_agent = request.headers.get("User-Agent", "")

    # Rate limiting checks
    now = time.time()
    if _appeal_locked.get(user_id):
        raise HTTPException(status_code=429, detail="You have already submitted an appeal. Please wait for a decision.")
    
    last_submit = _appeal_rate_limit.get(user_id, 0)
    if now - last_submit < APPEAL_COOLDOWN_SECONDS:
        raise HTTPException(status_code=429, detail=f"Please wait {APPEAL_COOLDOWN_SECONDS - (now - last_submit):.0f} seconds before submitting another appeal.")

    # IP rate limiting
    enforce_ip_rate_limit(ip)

    # Check remote cooldown
    remote_last = await get_remote_last_submit(user_id)
    if remote_last and now - remote_last < APPEAL_COOLDOWN_SECONDS:
        raise HTTPException(status_code=429, detail="You have recently submitted an appeal. Please wait for a decision.")

    # Generate appeal ID and mark as submitted
    appeal_id = str(uuid.uuid4())
    _appeal_rate_limit[user_id] = now
    _appeal_locked[user_id] = True
    _processed_appeals[appeal_id] = now

    # Translate appeal reason if needed
    appeal_reason_original = appeal_reason
    if user_lang != "en":
        appeal_reason = await translate_text(appeal_reason, target_lang="en", source_lang=user_lang)

    # Get message context
    message_cache = await fetch_message_cache(user_id)

    # Log appeal to database
    await log_appeal_to_supabase(
        appeal_id,
        user,
        ban_data.get("reason", "No reason provided"),
        "",  # ban_evidence would be populated from elsewhere
        appeal_reason,
        appeal_reason_original,
        user_lang,
        message_cache,
        hash_ip(ip),
        forwarded_for,
        user_agent,
    )

    # Log to Discord
    await send_log_message(
        f"New appeal from {user.get('username')}#{user.get('discriminator', '0')} ({user_id}) - "
        f"Reason: {ban_data.get('reason', 'No reason provided')} - "
        f"Appeal ID: {appeal_id}"
    )

    # Send DM to user if possible
    dm_sent = False
    if await ensure_dm_guild_membership(user_id):
        try:
            # This would be implemented with actual Discord bot DM sending
            # dm_sent = await send_appeal_confirmation_dm(user_id, appeal_id)
            pass
        except Exception as exc:
            logging.warning("Failed to send DM to user %s: %s", user_id, exc)
        finally:
            await maybe_remove_from_dm_guild(user_id)

    # Update appeal record with DM status
    await update_appeal_status(appeal_id, "pending", None, dm_sent)

    # Render confirmation page
    content = f"""
    <div class="card">
        <h2>{strings.get("appeal_submitted", "Appeal Submitted")}</h2>
        <p>{strings.get("appeal_submitted_msg", "Your appeal has been submitted successfully. We'll review it and get back to you soon.")}</p>
        <div class="callout">
            <strong>{strings.get("appeal_id", "Appeal ID")}:</strong> {appeal_id}
        </div>
        <div class="btn-row">
            <a href="/history" class="btn">{strings.get("view_status", "View Status")}</a>
            <a href="/" class="btn secondary">{strings.get("back_home", "Back Home")}</a>
        </div>
    </div>
    """
    return HTMLResponse(render_page(strings.get("appeal_submitted", "Appeal Submitted"), content, lang=user_lang, strings=strings))

async def handle_appeal_page(request: Request, session: dict, user: dict, user_lang: str, strings: dict):
    user_id = uid(user["id"])
    
    # Check if user is banned
    ban_data = await fetch_ban_if_exists(user_id)
    if not ban_data:
        content = f"""
        <div class="card">
            <h2>{strings.get("not_banned", "Not Banned")}</h2>
            <p>{strings.get("not_banned_msg", "You are not currently banned from the server.")}</p>
            <div class="btn-row">
                <a href="{INVITE_LINK}" class="btn">{strings.get("join_server", "Join Server")}</a>
                <a href="/" class="btn secondary">{strings.get("back_home", "Back Home")}</a>
            </div>
        </div>
        """
        return HTMLResponse(render_page(strings.get("not_banned", "Not Banned"), content, lang=user_lang, strings=strings))

    # Check if user has already appealed
    if _appeal_locked.get(user_id):
        content = f"""
        <div class="card">
            <h2>{strings.get("appeal_pending", "Appeal Pending")}</h2>
            <p>{strings.get("appeal_pending_msg", "You have already submitted an appeal. Please wait for a decision.")}</p>
            <div class="btn-row">
                <a href="/history" class="btn">{strings.get("view_status", "View Status")}</a>
                <a href="/" class="btn secondary">{strings.get("back_home", "Back Home")}</a>
            </div>
        </div>
        """
        return HTMLResponse(render_page(strings.get("appeal_pending", "Appeal Pending"), content, lang=user_lang, strings=strings))

    # Get message context
    message_cache = await fetch_message_cache(user_id)
    messages_html = ""
    if message_cache:
        messages_html = "<div class='chat-box'>"
        for msg in message_cache:
            timestamp = format_timestamp(msg.get("timestamp", ""))
            channel_name = msg.get("channel_name", "unknown")
            content = msg.get("content", "")
            messages_html += f"""
            <div class="chat-row">
                <div class="chat-time">
                    {timestamp}
                    <div class="chat-channel">#{channel_name}</div>
                </div>
                <div class="chat-content">{content}</div>
            </div>
            """
        messages_html += "</div>"
    else:
        messages_html = f"<p class='muted'>{strings.get('no_messages', 'No cached messages available.')}</p>"

    # Render appeal form
    content = f"""
    <div class="form-card">
        <h2>{strings.get("ban_details", "Ban Details")}</h2>
        <div class="callout">
            <strong>{strings.get("ban_reason", "Ban Reason")}:</strong> {ban_data.get("reason", "No reason provided")}
        </div>
        
        <h3>{strings.get("messages_header", "Recent Messages")}</h3>
        {messages_html}
        
        <form method="post" action="/submit">
            <div class="field">
                <label for="appeal_reason">{strings.get("appeal_reason", "Appeal Reason")}</label>
                <textarea id="appeal_reason" name="appeal_reason" required placeholder={strings.get("appeal_reason_placeholder", "Please explain why you believe the ban should be lifted...")}></textarea>
            </div>
            <div class="btn-row">
                <button type="submit" class="btn">{strings.get("submit_appeal", "Submit Appeal")}</button>
                <a href="/" class="btn secondary">{strings.get("cancel", "Cancel")}</a>
            </div>
        </form>
    </div>
    """
    return HTMLResponse(render_page(strings.get("appeal_form", "Appeal Form"), content, lang=user_lang, strings=strings))

async def handle_history_page(request: Request, session: dict, user: dict, user_lang: str, strings: dict):
    user_id = uid(user["id"])
    
    # Fetch appeal history
    history = await fetch_appeal_history(user_id)
    
    # Render history page
    history_html = render_history_items(history)
    
    content = f"""
    <div class="card">
        <h2>{strings.get("history_title", "Appeal History")}</h2>
        {history_html}
        <div class="btn-row">
            <a href="/" class="btn secondary">{strings.get("back_home", "Back Home")}</a>
        </div>
    </div>
    """
    return HTMLResponse(render_page(strings.get("history_title", "Appeal History"), content, lang=user_lang, strings=strings))