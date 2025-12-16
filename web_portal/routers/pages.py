from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
import uuid
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBearer
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..i18n import detect_language, get_strings, translate_text
from ..services import appeal_db, roblox_api, bloxlink_api
from ..services.discord_api import (
    ensure_dm_guild_membership,
    exchange_code_for_token,
    fetch_ban_if_exists,
    fetch_discord_user,
    fetch_guild_name,
    oauth_authorize_url as discord_oauth_authorize_url,
    post_appeal_embed,
    post_roblox_initial_appeal_embed,
    send_log_message,
    store_user_token,
)
from ..services.message_cache import fetch_message_cache
from ..services.security import enforce_ip_rate_limit, issue_state_token, validate_state_token
from ..services.sessions import (
    maybe_persist_session,
    persist_session,
    read_user_session,
    refresh_session_profile,
    serializer,
)
from ..services.supabase import (
    fetch_appeal_history,
    get_remote_last_submit,
    is_session_token_used,
    is_supabase_ready,
    log_appeal_to_supabase,
    mark_session_token,
    supabase_request,
)
from ..settings import (
    APPEAL_COOLDOWN_SECONDS,
    APPEAL_WINDOW_SECONDS,
    ROBLOX_SUPABASE_TABLE,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    STATUS_DATA_CACHE_TTL_SECONDS,
    SUPABASE_CONTEXT_TABLE,
    TARGET_GUILD_ID,
)
from ..state import _appeal_locked, _appeal_rate_limit, _ban_first_seen, _declined_users, _used_sessions
from ..ui import build_user_chip, render_history_items, render_page
from ..utils import (
    clean_display_name,
    format_relative,
    format_timestamp,
    get_client_ip,
    hash_ip,
    hash_value,
    normalize_language,
    shorten_public_ban_reason,
    simplify_ban_reason,
)

router = APIRouter()
security = HTTPBearer(auto_error=False)


class AppealService:
    """Service for handling appeal-related operations."""
    
    @staticmethod
    async def check_appeal_eligibility(user_id: str, ban_info: Dict[str, Any]) -> Tuple[bool, str]:
        """Check if a user is eligible to submit an appeal."""
        now = time.time()
        
        # Check if user was declined
        if _declined_users.get(user_id):
            return False, "Appeal declined"
        
        # Check if ban exists
        if not ban_info:
            return False, "No active ban"
        
        # Check appeal window
        first_seen = _ban_first_seen.get(user_id, now)
        _ban_first_seen[user_id] = first_seen
        window_expires_at = first_seen + APPEAL_WINDOW_SECONDS
        
        if now > window_expires_at:
            return False, "Appeal window closed"
        
        # Check if already appealed
        if _appeal_locked.get(user_id, False):
            return False, "Appeal already submitted"
        
        return True, ""
    
    @staticmethod
    async def check_rate_limit(user_id: str, ip: str) -> Tuple[bool, str]:
        """Check if user is rate limited."""
        now = time.time()
        
        # Check local rate limit
        last = _appeal_rate_limit.get(user_id)
        if last and now - last < APPEAL_COOLDOWN_SECONDS:
            wait = int(APPEAL_COOLDOWN_SECONDS - (now - last))
            return False, f"Please wait {wait} seconds before submitting another appeal."
        
        # Check remote rate limit
        remote_last = await get_remote_last_submit(user_id)
        if remote_last:
            last = max(last or 0, remote_last)
            if now - last < APPEAL_COOLDOWN_SECONDS:
                wait = int(APPEAL_COOLDOWN_SECONDS - (now - last))
                return False, f"Please wait {wait} seconds before submitting another appeal."
        
        return True, ""
    
    @staticmethod
    async def validate_session(session: str) -> Dict[str, Any]:
        """Validate and decode session token."""
        try:
            data = serializer.loads(session)
        except BadSignature:
            raise HTTPException(status_code=400, detail="Invalid session")
        
        now = time.time()
        issued_at = float(data.get("iat", 0))
        
        if not issued_at or now - issued_at > SESSION_TTL_SECONDS:
            raise HTTPException(status_code=400, detail="This form session expired. Please restart the appeal.")
        
        return data
    
    @staticmethod
    async def check_session_used(session_hash: str, user_id: str) -> bool:
        """Check if session has been used."""
        if _used_sessions.get(session_hash):
            return True
        
        return await is_session_token_used(session_hash)
    
    @staticmethod
    async def mark_session_used(session_hash: str, user_id: str):
        """Mark session as used."""
        now = time.time()
        _used_sessions[session_hash] = now
        await mark_session_token(session_hash, user_id, now)
        
        # Clean up stale sessions
        stale_sessions = [token for token, ts in _used_sessions.items() if now - ts > SESSION_TTL_SECONDS * 2]
        for token in stale_sessions:
            _used_sessions.pop(token, None)
    
    @staticmethod
    async def log_appeal_attempt(user_id: str, ip: str, lang: str, ban_reason: str, msg_ctx_len: int):
        """Log appeal attempt."""
        asyncio.create_task(
            send_log_message(
                f"[appeal_attempt] user={user_id} ip_hash={hash_ip(ip)} lang={lang} ban_reason=\"{ban_reason}\" msg_ctx={msg_ctx_len}"
            )
        )


class AuthService:
    """Service for handling authentication operations."""
    
    @staticmethod
    async def validate_state(request: Request, state: str) -> Dict[str, Any]:
        """Validate state token and return state data."""
        try:
            state_data = serializer.loads(state)
        except BadSignature:
            raise HTTPException(status_code=400, detail="Invalid state")
        
        ip = get_client_ip(request)
        state_id = state_data.get("state_id")
        
        if not validate_state_token(state_id, ip):
            raise HTTPException(status_code=400, detail="Invalid or replayed state")
        
        return state_data
    
    @staticmethod
    async def handle_discord_callback(request: Request, code: str, state: str, lang: Optional[str] = None) -> Dict[str, Any]:
        """Handle Discord OAuth callback."""
        state_data = await AuthService.validate_state(request, state)
        current_lang = normalize_language(lang or state_data.get("lang"))
        
        token = await exchange_code_for_token(code)
        user = await fetch_discord_user(token["access_token"])
        store_user_token(user["id"], token)
        
        ip = get_client_ip(request)
        asyncio.create_task(send_log_message(f"[auth] user={user['id']} ip_hash={hash_ip(ip)} lang={current_lang}"))
        
        return {
            "user": user,
            "lang": current_lang,
            "ip": ip,
            "state_data": state_data
        }
    
    @staticmethod
    async def handle_roblox_callback(request: Request, code: str, state: str, lang: Optional[str] = None) -> Dict[str, Any]:
        """Handle Roblox OAuth callback."""
        state_data = await AuthService.validate_state(request, state)
        current_lang = normalize_language(lang or state_data.get("lang"))
        
        token = await roblox_api.exchange_code_for_token(code)
        try:
            user = await roblox_api.get_user_info(token["access_token"])
        except httpx.HTTPStatusError as exc:
            logger.warning(f"Roblox user info fetch failed: {exc} | body={exc.response.text}")
            raise HTTPException(status_code=422, detail="Failed to retrieve Roblox user information. The provided code might be invalid or expired. Please try again.") from exc
        user_id = user["sub"]
        
        # Now that we have the user_id, we can properly store the token
        await roblox_api.store_roblox_token(user_id, token)

        ip = get_client_ip(request)
        asyncio.create_task(send_log_message(f"[auth_roblox] user={user_id} ip_hash={hash_ip(ip)} lang={current_lang}"))
        
        return {
            "user": user,
            "lang": current_lang,
            "ip": ip,
            "state_data": state_data
        }


class PageRenderer:
    """Service for rendering HTML pages."""
    
    @staticmethod
    async def render_home_page(request: Request, lang: Optional[str] = None) -> HTMLResponse:
        """Render the home page."""
        current_lang = await detect_language(request, lang)
        strings = await get_strings(current_lang)
        ip = get_client_ip(request)
        state_token = issue_state_token(ip)
        state = serializer.dumps({
            "nonce": secrets.token_urlsafe(8), 
            "lang": current_lang, 
            "state_id": state_token
        })
        
        asyncio.create_task(send_log_message(f"[visit_home] ip_hash={hash_ip(ip)} lang={current_lang}"))
        
        user_session = read_user_session(request)
        user_session, session_refreshed = await refresh_session_profile(user_session)
        strings = dict(strings)
        
        discord_login_url = discord_oauth_authorize_url(state)
        roblox_login_url = roblox_api.oauth_authorize_url(state)
        
        strings["top_actions"] = build_user_chip(
            user_session, 
            discord_login_url=discord_login_url, 
            roblox_login_url=roblox_login_url
        )
        
        content = await PageRenderer._build_home_content(
            strings, discord_login_url, roblox_login_url, user_session
        )
        
        strings["script_nonce"] = secrets.token_urlsafe(12)
        strings["script_block"] = PageRenderer._get_home_script()
        
        response = HTMLResponse(
            render_page("BlockSpin — Appeals", content, lang=current_lang, strings=strings), 
            headers={"Cache-Control": "no-store"}
        )
        maybe_persist_session(request, response, user_session, session_refreshed)
        response.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return response
    
    @staticmethod
    async def _build_home_content(
        strings: Dict[str, str], 
        discord_login_url: str, 
        roblox_login_url: str, 
        session: Optional[Dict[str, Any]] = None
    ) -> str:
        """Build the content for the home page."""
        
        session = session or {}
        has_discord = "uid" in session
        has_roblox = "ruid" in session

        # Action buttons in the hero section
        hero_actions = []
        if has_discord and has_roblox:
            # Both linked, no appeal needed here.
            pass
        elif has_discord:
            hero_actions.append(f"""
                <a class="btn btn--primary" href="{html.escape(discord_login_url)}" aria-label="Appeal with Discord">
                    Appeal with Discord
                </a>
            """)
        elif has_roblox:
            hero_actions.append(f"""
                <a class="btn btn--primary" href="{html.escape(roblox_login_url)}" aria-label="Appeal with Roblox">
                    Appeal with Roblox
                </a>
            """)
        else:
            # Neither linked, show both options
            hero_actions.append(f"""
                <a class="btn btn--primary" href="{html.escape(discord_login_url)}" aria-label="Appeal with Discord">
                    Appeal with Discord
                </a>
            """)
            hero_actions.append(f"""
                <a class="btn btn--soft" href="{html.escape(roblox_login_url)}" aria-label="Appeal with Roblox">
                    Appeal with Roblox
                </a>
            """)
        
        if not hero_actions:
            pass

        # Action buttons in the side panel
        panel_actions = []
        if not has_discord and has_roblox:
             # Roblox is linked, but Discord is not. Prompt to connect Discord.
            panel_actions.append(
                f'<p class="muted">To see your full appeal history, connect your Discord account.</p>'
                f'<a class="btn btn--discord btn--wide" href="{html.escape(discord_login_url)}">Connect Discord</a>'
            )
        elif has_discord and not has_roblox:
            # Discord is linked, but Roblox is not. Prompt to connect Roblox.
            panel_actions.append(
                f'<p class="muted">To see your full appeal history, connect your Roblox account.</p>'
                f'<a class="btn btn--roblox btn--wide" href="{html.escape(roblox_login_url)}">Connect Roblox</a>'
            )
        elif not has_discord:
            panel_actions.append(
                f'<a class="btn btn--discord btn--wide" href="{html.escape(discord_login_url)}">Continue with Discord</a>'
            )
        if not has_roblox: # This 'if' should be separate if we want both options when neither are linked
            if not has_discord: # Only show Roblox option if Discord is also not linked initially
                panel_actions.append(
                    f'<a class="btn btn--soft btn--wide" href="{html.escape(roblox_login_url)}">Continue with Roblox</a>'
                )

        if not panel_actions:
            panel_actions.append('<p class="muted">You are signed into both platforms.</p><a class="btn btn--soft btn--wide" href="/status">Check Appeal Status</a>')

        return f"""
        <section class="hero">
          <div class="hero__card">
            <div class="hero__badge">
              <span class="pulse" aria-hidden="true"></span>
              Live moderation workflow
            </div>

            <h1 class="hero__title">
              Resolve your ban the <span class="shine">right way</span>.
            </h1>

            <p class="hero__sub">
              Authenticate with Discord or Roblox, review your status, and submit a clear, respectful appeal to BlockSpin moderators.
            </p>

            <div class="hero__cta">
              {"".join(hero_actions)}
            </div>

            <div class="hero__meta">
              <div class="stat">
                <div class="stat__k">Live status</div>
                <div class="stat__v" id="liveStatus">Checking…</div>
              </div>
              <div class="stat">
                <div class="stat__k">Latest ref</div>
                <div class="stat__v" id="liveRef">—</div>
              </div>
              <div class="stat">
                <div class="stat__k">Decision</div>
                <div class="stat__v" id="liveDecision">—</div>
              </div>
            </div>
          </div>

          <div class="hero__side">
            <div class="panel">
              <h2 class="panel__title">How it works</h2>
              <ol class="steps">
                <li><span class="steps__n">1</span> Sign in to confirm your identity.</li>
                <li><span class="steps__n">2</span> Review your appeal status + history.</li>
                <li><span class="steps__n">3</span> Submit one appeal with concise evidence.</li>
              </ol>
              <div class="panel__note">
                Tip: "I understand what I did, here's context, here's how I'll improve" is the fastest path.
              </div>

              <div class="panel__actions">
                {"".join(panel_actions)}
                <div class="legal">
                  <a href="/tos">Terms</a>
                  <span class="dot" aria-hidden="true"></span>
                  <a href="/privacy">Privacy</a>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section class="grid">
          <article class="card">
            <div class="card__top">
              <h2 class="card__title">Appeal history</h2>
              <div class="chip" id="historyChip">Loading…</div>
            </div>

            <div class="empty" id="historyEmpty">
              Sign in to see your history and live status.
              <div class="empty__actions">
                <a class="btn btn--soft" href="/status">Open Status</a>
              </div>
            </div>

            <ul class="list" id="historyList" hidden></ul>
          </article>

          <article class="card">
            <div class="card__top">
              <h2 class="card__title">Status signals</h2>
              <div class="chip chip--ok" id="signalChip">Portal online</div>
            </div>

            <div class="kv">
              <div class="kv__row">
                <div class="kv__k">Live feed</div>
                <div class="kv__v" id="feedState">Connected</div>
              </div>
              <div class="kv__row">
                <div class="kv__k">Updates</div>
                <div class="kv__v">Every 15s</div>
              </div>
              <div class="kv__row">
                <div class="kv__k">Privacy</div>
                <div class="kv__v">Minimal display</div>
              </div>
            </div>

            <div class="callout">
              Appeals are reviewed by moderators. Decisions may be final. Don't spam submissions.
            </div>
          </article>
        </section>
        """
    
    @staticmethod
    def _get_home_script() -> str:
        """Get the JavaScript for the home page."""
        return """
        (function(){
          const els = {
            liveStatus: document.getElementById("liveStatus"),
            liveRef: document.getElementById("liveRef"),
            liveDecision: document.getElementById("liveDecision"),
            list: document.getElementById("historyList"),
            empty: document.getElementById("historyEmpty"),
            chip: document.getElementById("historyChip"),
            feedState: document.getElementById("feedState"),
            signalChip: document.getElementById("signalChip"),
          };

          function esc(s){ return String(s ?? "").replace(/[&<>"'\\/]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;','/':'&#x2F;'}[m])); }
          function statusClass(status){
            const t = String(status || "pending").toLowerCase();
            if (t.startsWith("accept")) return "ok";
            if (t.startsWith("decline")) return "no";
            return "wait";
          }
          function statusLabel(status){
            const t = String(status || "pending").toLowerCase();
            if (t.startsWith("accept")) return "Accepted";
            if (t.startsWith("decline")) return "Declined";
            return "Pending";
          }

          async function tick(){
            try{
              const r = await fetch("/status/data", { headers: { "Accept":"application/json" } });
              const data = await r.json();
              const hist = Array.isArray(data.history) ? data.history : [];

              if (!hist.length){
                if (els.liveStatus) els.liveStatus.textContent = "Sign in required";
                if (els.liveRef) els.liveRef.textContent = "—";
                if (els.liveDecision) els.liveDecision.textContent = "—";
                if (els.chip) els.chip.textContent = "No data";
                if (els.empty) els.empty.hidden = false;
                if (els.list) els.list.hidden = true;
                if (els.feedState) els.feedState.textContent = "Idle";
                return;
              }

              const latest = hist[0] || {};
              if (els.liveStatus) els.liveStatus.textContent = "Active";
              if (els.liveRef) els.liveRef.textContent = latest.appeal_id ? String(latest.appeal_id) : "—";
              if (els.liveDecision) els.liveDecision.textContent = statusLabel(latest.status);
              if (els.chip) els.chip.textContent = `${hist.length} recent`;

              if (els.empty) els.empty.hidden = true;
              if (els.list) els.list.hidden = false;

              if (els.list){
                els.list.innerHTML = hist.map(item => {
                  const s = statusLabel(item.status);
                  const cls = statusClass(item.status);
                  return `
                    <li class="row">
                      <div class="row__left">
                        <div class="pill pill--${cls}">${esc(s)}</div>
                        <div class="row__meta">
                          <div class="row__k">Reference</div>
                          <div class="row__v">${esc(item.appeal_id || "—")}</div>
                        </div>
                        <div class="row__meta">
                          <div class="row__k">Submitted</div>
                          <div class="row__v">${esc(item.created_at || "—")}</div>
                        </div>
                      </div>
                      <div class="row__right">
                        <div class="row__k">Ban reason</div>
                        <div class="row__v row__v--wrap">${esc(item.ban_reason || "—")}</div>
                      </div>
                    </li>
                  `;
                }).join("");
              }

              if (els.feedState) els.feedState.textContent = "Connected";
              if (els.signalChip){
                els.signalChip.textContent = "Portal online";
                els.signalChip.classList.remove("chip--warn");
                els.signalChip.classList.add("chip--ok");
              }
            }catch(e){
              if (els.feedState) els.feedState.textContent = "Disconnected";
              if (els.signalChip){
                els.signalChip.textContent = "Live updates unavailable";
                els.signalChip.classList.add("chip--warn");
                els.signalChip.classList.remove("chip--ok");
              }
              if (els.liveStatus) els.liveStatus.textContent = "Unavailable";
            }
          }

          tick();
          setInterval(tick, 15000);
        })();
        """
    
    @staticmethod
    async def render_status_page(request: Request, lang: Optional[str] = None) -> HTMLResponse:
        """Render the status page."""
        current_lang = await detect_language(request, lang)
        strings = await get_strings(current_lang)
        ip = get_client_ip(request)
        asyncio.create_task(send_log_message(f"[visit_status] ip_hash={hash_ip(ip)} lang={current_lang}"))
        
        session = read_user_session(request)
        session, session_refreshed = await refresh_session_profile(session)
        strings = dict(strings)
        
        if not session:
            state_token = issue_state_token(ip)
            state = serializer.dumps({
                "nonce": secrets.token_urlsafe(8), 
                "lang": current_lang, 
                "state_id": state_token
            })
            discord_login_url = discord_oauth_authorize_url(state)
            roblox_login_url = roblox_api.oauth_authorize_url(state)
            
            strings["top_actions"] = build_user_chip(
                None, discord_login_url=discord_login_url, roblox_login_url=roblox_login_url
            )
            
            content = f"""
              <div class="card status danger">
                <h1 style="margin-bottom:10px;">Sign in required</h1>
                <p class="muted">Sign in to view your BlockSpin appeal history and live status.</p>
                <a class="btn btn--discord" href="{discord_login_url}"><span class="btn__icon" aria-hidden="true">⌁</span>{strings['login']}</a>
                 <a class="btn btn--roblox" href="{roblox_login_url}">{strings['login_roblox']}</a>
              </div>
            """
            
            resp = HTMLResponse(
                render_page("Appeal status", content, lang=current_lang, strings=strings), 
                status_code=401, 
                headers={"Cache-Control": "no-store"}
            )
            resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
            return resp
        
        strings["top_actions"] = build_user_chip(session)
        
        user_id = session.get("uid") or session.get("ruid")
        if is_supabase_ready():
            history = await fetch_appeal_history(user_id, limit=10)
            history_html = render_history_items(history, format_timestamp=format_timestamp)
        else:
            history_html = "<div class='muted'></div>"
        
        content = f"""
          <div class="card status-card">
            <div class="status-heading">
              <h1>Appeal history for {html.escape(clean_display_name(session.get('display_name') or session.get('uname','you')))}</h1>
              <p class="muted">Monitor decisions and peer context for this ban review.</p>
            </div>
            {history_html}
            <div class="btn-row" style="margin-top:10px;">
              <a class="btn secondary" href="/">Back home</a>
            </div>
          </div>
        """
        
        resp = HTMLResponse(
            render_page("Appeal status", content, lang=current_lang, strings=strings), 
            headers={"Cache-Control": "no-store"}
        )
        maybe_persist_session(request, resp, session, session_refreshed)
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp
    
        @staticmethod
    
        async def render_discord_appeal_page(
    
            request: Request, 
    
            user: Dict[str, Any],
    
            ban: Dict[str, Any],
    
            message_cache: List[Dict[str, Any]],
    
            session_token: str,
    
            current_lang: str,
    
            strings: Dict[str, str],
    
            current_session: Optional[Dict[str, Any]] = None # Added parameter
    
        ) -> HTMLResponse:
    
            """Render the Discord appeal page."""
    
            uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
    
            display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
    
            
    
            now = time.time()
    
            first_seen = _ban_first_seen.get(user["id"], now)
    
            window_expires_at = first_seen + APPEAL_WINDOW_SECONDS
    
            
    
            guild_name = await fetch_guild_name(str(TARGET_GUILD_ID))
    
            
    
            uname = html.escape(uname_label)
    
            ban_reason_raw = simplify_ban_reason(ban.get("reason")) or "No reason provided."
    
            ban_reason = html.escape(ban_reason_raw)
    
            user_id_label = html.escape(str(user["id"]))
    
            ban_observed_rel = html.escape(format_relative(now - first_seen))
    
            ban_observed_at = html.escape(format_timestamp(int(first_seen)))
    
            appeal_deadline = html.escape(format_timestamp(int(window_expires_at)))
    
            
    
            message_cache_html = ""
    
            if message_cache:
    
                msgs_to_show = list(reversed(message_cache))
    
                rows = []
    
                for m in msgs_to_show:
    
                    ts = html.escape(format_timestamp(m.get("timestamp")))
    
                    content = html.escape(m.get("content") or "")
    
                    channel = html.escape(m.get("channel_name") or "#channel")
    
                    rows.append(
    
                        f"""
    
                        <div class='chat-row'>
    
                            <div class='chat-time'>{ts} <span class='chat-channel'>{channel}</span></div>
    
                            <div class='chat-content'>{content}</div>
    
                        </div>
    
                        """
    
                    )
    
                message_cache_html = f'''<div class="chat-box">{" ".join(rows)}</div>'''
    
            else:
    
                message_cache_html = f'''<div class='muted' style='padding:10px; border:1px dashed var(--border); border-radius:8px;'>{strings['no_messages']}</div>'''
    
            
    
            context_count = len(message_cache) if message_cache else 0
    
            context_open = "open" if context_count else ""
    
            
    
            window_script = """
    
              <script>
    
                (function(){
    
                  const el = document.getElementById('appealWindowRemaining');
    
                  if(!el) return;
    
                  const expiresSeconds = parseInt(el.dataset.expires || '0', 10);
    
                  if(!expiresSeconds) return;
    
                  const expiresMs = expiresSeconds * 1000;
    
                  function format(ms){
    
                    const total = Math.max(0, Math.floor(ms / 1000));
    
                    const days = Math.floor(total / 86400);
    
                    const hours = Math.floor((total % 86400) / 3600);
    
                    return `${days}d ${hours}h`;
    
                  }
    
                  function tick(){
    
                    el.textContent = format(expiresMs - Date.now());
    
                  }
    
                  tick();
    
                  setInterval(tick, 30000);
    
                })();
    
              </script>
    
            """
    
            
    
            roblox_notice = ""
    
            # Use current_session if provided, otherwise read from request
    
            session_for_notice = current_session if current_session is not None else read_user_session(request)
    
            if not session_for_notice or not session_for_notice.get("ruid"):
    
                ip = get_client_ip(request)
    
                state_token = issue_state_token(ip)
    
                state = serializer.dumps({
    
                    "nonce": secrets.token_urlsafe(8), 
    
                    "lang": current_lang, 
    
                    "state_id": state_token,
    
                    "context": "discord_appeal", # Context for Roblox callback
    
                    "discord_user_id": user["id"]
    
                })
    
                roblox_login_url = roblox_api.oauth_authorize_url(state)
    
                roblox_notice = f"""
    
                <div class="callout callout--warn">
    
                    <p><strong>Want updates on your appeal?</strong></p>
    
                    <p>Connect your Roblox account to view combined appeal history and updates via the web portal.</p>
    
                    <a class="btn btn--roblox" href="{html.escape(roblox_login_url)}">Connect Roblox</a>
    
                </div>
    
                """
    
            
    
            content = f"""
    
              <div class="grid-2">
    
                <div class="form-card">
    
                  <div class="badge">Window remaining: <span id="appealWindowRemaining" data-expires="{int(window_expires_at)}"></span></div>
    
                  <h2 style="margin:8px 0;">Appeal your BlockSpin ban</h2>
    
                  <p class="muted">One appeal per ban. Include context, evidence, and what you will change.</p>
    
                  {roblox_notice}
    
                  <form class="form" action="/submit" method="post">
    
                    <input type="hidden" name="session" value="{html.escape(session_token)}" />
    
                    <div class="field">
    
                      <label for="evidence">Ban evidence (optional)</label>
    
                      <input name="evidence" type="text" placeholder="Links or notes you have" />
    
                    </div>
    
                    <div class="field">
    
                      <label for="appeal_reason">Why should you be unbanned?</label>
    
                      <textarea name="appeal_reason" required placeholder="Be concise. What happened, and what will be different next time?"></textarea>
    
                    </div>
    
                    <button class="btn" type="submit">Submit appeal</button>
    
                  </form>
    
                </div>
    
                <div class="card">
    
                  <details class="details" open>
    
                    <summary>{strings['ban_details']}</summary>
    
                    <div class="details-body">
    
                      <div class="kv">
    
                        <div class="kv-row"><div class="k">User</div><div class="v">{uname}</div></div>
    
                        <div class="kv-row"><div class="k">User ID</div><div class="v">{user_id_label}</div></div>
    
                        <div class="kv-row"><div class="k">Server</div><div class="v">{html.escape(guild_name or 'BlockSpin')}</div></div>
    
                        <div class="kv-row"><div class="k">Ban observed</div><div class="v">{ban_observed_rel} · {ban_observed_at}</div></div>
    
                        <div class="kv-row"><div class="k">Appeal deadline</div><div class="v">{appeal_deadline}</div></div>
    
                        <div class="kv-row"><div class="k">Reason</div><div class="v">{ban_reason}</div></div>
    
                      </div>
    
                    </div>
    
                  </details>
    
    
    
                  <details class="details" {context_open}>
    
                    <summary>{strings['messages_header']} <span style="color:var(--muted2); font-weight:700; letter-spacing:0; text-transform:none;">({context_count})</span></summary>
    
                    <div class="details-body">{message_cache_html}</div>
    
                  </details>
    
    
    
                  <details class="details">
    
                    <summary>Your history</summary>
    
                    <div class="details-body">{render_history_items([], format_timestamp=format_timestamp)}</div>
    
                  </details>
    
                  <div class="btn-row" style="margin-top:10px;">
    
                    <a class="btn secondary" href="/">Back home</a>
    
                  </div>
    
                </div>
    
              </div>
    
              {window_script}
    
            """
    
            
    
            resp = HTMLResponse(
    
                render_page("Appeal your ban", content, lang=current_lang, strings=strings), 
    
                status_code=200, 
    
                headers={"Cache-Control": "no-store"}
    
            )
    
            # Note: The session is persisted in the main callback handler, not here.
    
            # persist_user_session(request, resp, user["id"], uname_label, display_name=display_name) # Removed
    
            resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
    
            return resp
    
    @staticmethod
    async def render_roblox_appeal_page(
        request: Request, 
        user: Dict[str, Any], 
        ban: Dict[str, Any], 
        session_token: str,
        current_lang: str,
        strings: Dict[str, str]
    ) -> HTMLResponse:
        """Render the Roblox appeal page."""
        user_id = user["sub"]
        uname_label = user.get("name") or user.get("preferred_username")
        display_name = clean_display_name(user.get("nickname") or uname_label)
        
        ban_history = await roblox_api.get_ban_history(user_id)
        short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")
        
        ban_reason = html.escape(short_reason)
        user_id_label = html.escape(str(user_id))

        discord_notice = ""
        session = read_user_session(request)
        if not session or not session.get("uid"):
            ip = get_client_ip(request)
            state_token = issue_state_token(ip)
            state = serializer.dumps({
                "nonce": secrets.token_urlsafe(8), 
                "lang": current_lang, 
                "state_id": state_token,
                "context": "roblox_appeal",
                "roblox_user_id": user_id
            })
            discord_login_url = discord_oauth_authorize_url(state)
            discord_notice = f"""
            <div class="callout callout--warn">
                <p><strong>Want updates on your appeal?</strong></p>
                <p>Sign in with Discord to receive status updates via DM.</p>
                <a class="btn btn--discord" href="{html.escape(discord_login_url)}">Connect Discord</a>
            </div>
            """
        
        content = f"""
          <div class="grid-2">
            <div class="form-card">
              <h2 style="margin:8px 0;">Appeal your Roblox Ban</h2>
              <p class="muted">One appeal per ban. Be clear and concise.</p>
              {discord_notice}
              <form class="form" action="/roblox/submit" method="post">
                <input type="hidden" name="session" value="{html.escape(session_token)}" />
                <div class="field">
                  <label for="appeal_reason">Why should you be unbanned?</label>
                  <textarea name="appeal_reason" required placeholder="Explain what happened and why you should be allowed back."></textarea>
                </div>
                <button class="btn btn--roblox" type="submit">Submit Appeal</button>
              </form>
            </div>
            <div class="card">
              <details class="details" open>
                <summary>Ban Details</summary>
                <div class="details-body">
                  <div class="kv">
                    <div class="kv-row"><div class="k">User</div><div class="v">{html.escape(uname_label)}</div></div>
                    <div class="kv-row"><div class="k">User ID</div><div class="v">{user_id_label}</div></div>
                    <div class="kv-row"><div class="k">Reason</div><div class="v">{ban_reason}</div></div>
                  </div>
                </div>
              </details>
            </div>
          </div>
        """
        
        resp = HTMLResponse(
            render_page("Appeal your Roblox Ban", content, lang=current_lang, strings=strings), 
            status_code=200, 
            headers={"Cache-Control": "no-store"}
        )
        persist_roblox_user_session(request, resp, user_id, uname_label, display_name=display_name)
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp


# Route handlers
@router.get("/", response_class=HTMLResponse)
async def home(request: Request, lang: Optional[str] = None):
    """Render the home page."""
    return await PageRenderer.render_home_page(request, lang)


@router.get("/tos", response_class=HTMLResponse)
async def tos():
    """Render the Terms of Service page."""
    content = """
      <div class="card">
        <h2>Terms of Service</h2>
        <p class="muted">BlockSpin appeals are a formal process. By using this portal you agree to provide accurate information and accept that moderators may make irreversible decisions.</p>
        <p class="muted"><strong>What you must do:</strong> submit truthful details, include relevant context, and avoid duplicate or spam appeals.</p>
        <p class="muted"><strong>What is prohibited:</strong> ban evasion attempts, falsified evidence, harassment of staff, automated submissions, or sharing this portal for abuse.</p>
        <p class="muted"><strong>Enforcement:</strong> violations may result in denial of appeals, additional sanctions, or permanent denial of future appeals.</p>
        <p class="muted"><strong>Logging:</strong> we capture appeal content, account identifiers, IP/network metadata, and basic device info solely to secure the process.</p>
        <div class="btn-row" style="margin-top:10px;"><a class="btn secondary" href="/">Back home</a></div>
      </div>
    """
    return HTMLResponse(render_page("Terms of Service", content), headers={"Cache-Control": "no-store"})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    """Render the Privacy Policy page."""
    content = """
      <div class="card">
        <h2>Privacy</h2>
        <p class="muted"><strong>Data we collect:</strong> appeal submissions, account identifiers, IP, approximate region, basic device/user agent, and limited message context to verify events.</p>
        <p class="muted"><strong>How we use it:</strong> secure authentication, fraud prevention, moderation review, and auditability.</p>
        <p class="muted"><strong>Sharing:</strong> only with authorized BlockSpin staff or as required by law. We do not sell your data.</p>
        <p class="muted"><strong>Retention:</strong> data is kept for security and compliance; requests for removal can be directed to moderators subject to policy and legal obligations.</p>
        <div class="btn-row" style="margin-top:10px;"><a class="btn secondary" href="/">Back home</a></div>
      </div>
    """
    return HTMLResponse(render_page("Privacy", content), headers={"Cache-Control": "no-store"})


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, lang: Optional[str] = None):
    """Render the status page, which will fetch data from /status/data."""
    current_lang = await detect_language(request, lang)
    strings = await get_strings(current_lang)
    ip = get_client_ip(request)
    asyncio.create_task(send_log_message(f"[visit_status] ip_hash={hash_ip(ip)} lang={current_lang}"))

    session = read_user_session(request)
    session, session_refreshed = await refresh_session_profile(session)
    strings = dict(strings)

    if not session:
        state_token = issue_state_token(ip)
        state = serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang, "state_id": state_token})
        discord_login_url = discord_oauth_authorize_url(state)
        roblox_login_url = roblox_api.oauth_authorize_url(state)
        
        strings["top_actions"] = build_user_chip(None, discord_login_url=discord_login_url, roblox_login_url=roblox_login_url)
        
        content = f"""
          <div class="card status danger">
            <h1 style="margin-bottom:10px;">Sign in required</h1>
            <p class="muted">Sign in to view your appeal history and live status.</p>
            <a class="btn btn--discord" href="{discord_login_url}"><span class="btn__icon" aria-hidden="true">⌁</span>{strings['login']}</a>
            <a class="btn btn--roblox" href="{roblox_login_url}">{strings['login_roblox']}</a>
          </div>
        """
        
        resp = HTMLResponse(render_page("Appeal status", content, lang=current_lang, strings=strings), status_code=401, headers={"Cache-Control": "no-store"})
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp

    discord_login_url = None
    roblox_login_url = None
    if not session.get("uid") or not session.get("ruid"):
        state_token = issue_state_token(ip)
        state = serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang, "state_id": state_token})
        discord_login_url = discord_oauth_authorize_url(state)
        roblox_login_url = roblox_api.oauth_authorize_url(state)

    strings["top_actions"] = build_user_chip(
        session,
        discord_login_url=discord_login_url,
        roblox_login_url=roblox_login_url
    )
    display_name = html.escape(clean_display_name(session.get('display_name') or session.get('uname', 'you')))

    # The history is now loaded dynamically by the frontend from /status/data
    history_html = """
    <div id="history-container">
        <div class="loading">Loading history...</div>
    </div>
    <script>
        fetch('/status/data')
            .then(response => response.json())
            .then(data => {
                const container = document.getElementById('history-container');
                if (data.history && data.history.length > 0) {
                    container.innerHTML = `<ul>${data.history.map(item => `
                        <li class="row">
                            <div class="row__left">
                                <div class="pill pill--${item.status === 'accepted' ? 'ok' : item.status === 'declined' ? 'no' : 'wait'}">${item.status}</div>
                                <div class="row__meta">
                                    <div class="row__k">Platform</div>
                                    <div class="row__v">${item.platform}</div>
                                </div>
                                <div class="row__meta">
                                    <div class="row__k">Submitted</div>
                                    <div class="row__v">${new Date(item.created_at).toLocaleString()}</div>
                                </div>
                            </div>
                            <div class="row__right">
                                <div class="row__k">Reason</div>
                                <div class="row__v row__v--wrap">${item.ban_reason || 'N/A'}</div>
                            </div>
                        </li>
                    `).join('')}</ul>`;
                } else {
                    container.innerHTML = '<div class="muted">No appeal history found.</div>';
                }
            })
            .catch(error => {
                const container = document.getElementById('history-container');
                container.innerHTML = '<div class="danger">Failed to load appeal history.</div>';
                console.error('Error fetching appeal history:', error);
            });
    </script>
    """

    content = f"""
      <div class="card status-card">
        <div class="status-heading">
          <h1>Appeal history for {display_name}</h1>
          <p class="muted">Monitor decisions and peer context for your ban reviews.</p>
        </div>
    """

    linking_prompt = ""
    if not session.get("uid") and session.get("ruid"):
        # Roblox linked, Discord not
        state_token = issue_state_token(ip)
        state = serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang, "state_id": state_token})
        discord_login_url = discord_oauth_authorize_url(state)
        linking_prompt = f"""
            <div class="callout callout--info" style="margin-bottom: 20px;">
                <p><strong>Connect your Discord account to see your full appeal history!</strong></p>
                <p>Link your Discord account to view combined appeals and receive updates via DM.</p>
                <a class="btn btn--discord" href="{html.escape(discord_login_url)}">Connect Discord</a>
            </div>
        """
    elif session.get("uid") and not session.get("ruid"):
        # Discord linked, Roblox not
        state_token = issue_state_token(ip)
        state = serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang, "state_id": state_token})
        roblox_login_url = roblox_api.oauth_authorize_url(state)
        linking_prompt = f"""
            <div class="callout callout--info" style="margin-bottom: 20px;">
                <p><strong>Connect your Roblox account to see your full appeal history!</strong></p>
                <p>Link your Roblox account to view combined appeals and ensure all your appeals are visible.</p>
                <a class="btn btn--roblox" href="{html.escape(roblox_login_url)}">Connect Roblox</a>
            </div>
        """
    
    content += linking_prompt
    content += history_html
    content += f"""
        <div class="btn-row" style="margin-top:10px;">
          <a class="btn secondary" href="/">Back home</a>
        </div>
      </div>
    """
    
    resp = HTMLResponse(render_page("Appeal status", content, lang=current_lang, strings=strings), headers={"Cache-Control": "no-store"})
    maybe_persist_session(request, resp, session, session_refreshed)
    resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
    return resp


@router.get("/status/data")
async def get_status_data(request: Request):
    """Endpoint to fetch combined appeal history for the logged-in user."""
    session = read_user_session(request)
    if not session:
        return {"history": []}

    discord_user_id = session.get("uid")
    roblox_user_id = session.get("ruid")

    all_appeals = []

    # Fetch Discord-specific appeals
    if discord_user_id:
        discord_appeals = await fetch_appeal_history(discord_user_id, limit=25)
        for item in discord_appeals:
            item['platform'] = 'Discord'
            all_appeals.append(item)

    # Fetch Roblox appeals (can be linked to discord_user_id OR roblox_user_id)
    if roblox_user_id or discord_user_id:
        roblox_appeals = await appeal_db.get_roblox_appeal_history(
            roblox_id=roblox_user_id, 
            discord_user_id=discord_user_id, 
            limit=50
        )
        for item in roblox_appeals:
            item['platform'] = 'Roblox'
            item['ban_reason'] = item.get('short_ban_reason')
            item['appeal_id'] = item.get('id')
            all_appeals.append(item)
    
    # Use a dictionary to create a unique list of appeals.
    # The key is a tuple of (platform, id) to ensure uniqueness across different appeal types.
    unique_appeals = {}
    for item in all_appeals:
        platform = item.get("platform")
        # For Roblox, the unique ID is 'id'. For Discord, it's 'appeal_id'.
        item_id = item.get("id") if platform == "Roblox" else item.get("appeal_id")
        if platform and item_id:
            unique_appeals[(platform, item_id)] = item
    
    # Sort the unique appeals by creation date
    sorted_appeals = sorted(list(unique_appeals.values()), key=lambda x: x.get("created_at", "0"), reverse=True)

    return {"history": sorted_appeals[:50]}


@router.get("/logout")
async def logout():
    """Handle logout."""
    resp = RedirectResponse("/")
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp



@router.get("/callback")
async def callback(request: Request, code: str, state: str, lang: Optional[str] = None):
    """Handle Discord OAuth callback."""
    auth_data = await AuthService.handle_discord_callback(request, code, state, lang)
    user = auth_data["user"]
    current_lang = auth_data["lang"]
    state_data = auth_data["state_data"] # Extract state_data

    strings = await get_strings(current_lang)
    strings = dict(strings)

    uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
    display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)

    # Check if a Roblox session already exists AND if this Discord auth came from a Roblox appeal context
    roblox_session = read_user_session(request)
    logging.info(f"Discord callback: Current session in request: {roblox_session}")
    if roblox_session and "ruid" in roblox_session and state_data.get("context") == "roblox_appeal":
        roblox_user_id = roblox_session["ruid"]
        roblox_uname_label = roblox_session.get("runame") or ""
        roblox_display_name = roblox_session.get("rdisplay_name") or ""

        # Fetch Roblox ban status again
        ban = await roblox_api.get_live_ban_status(roblox_user_id)
        if not ban:
            # If no ban found, redirect to home or status with both accounts linked
            response = RedirectResponse("/status")
            persist_user_session(request, response, user["id"], uname_label, display_name=display_name)
            return response
        
        # Fetch ban history
        ban_history = await roblox_api.get_ban_history(roblox_user_id)
        short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")

        # Create a response object first for cookie setting
        response = RedirectResponse("/status")
        
        # Ensure both Discord and Roblox sessions are persisted
        updated_session = persist_session( # Capture return value
            request, response,
            discord_user_id=user["id"],
            discord_username=uname_label,
            discord_display_name=display_name,
            roblox_user_id=roblox_user_id,
            roblox_username=roblox_uname_label,
            roblox_display_name=roblox_display_name
        )
        return response


    # Check if user is eligible to appeal
    ban = await fetch_ban_if_exists(user["id"])
    eligible, reason = await AppealService.check_appeal_eligibility(user["id"], ban)
    
    # Create a response object first for cookie setting
    response = HTMLResponse(status_code=200) # Default, will be updated by render_page
    
    updated_session = persist_session( # Capture return value
        request, response,
        discord_user_id=user["id"],
        discord_username=uname_label,
        discord_display_name=display_name
    )

    if not eligible:
        if reason == "Appeal declined":
            content = f"""
              <div class="card status danger">
                <h1 style="margin-bottom:10px;">Appeal declined</h1>
                <p>{html.escape(user['username'])}, your previous appeal was declined. Further appeals are blocked.</p>
                <a class="btn" href="/">Return home</a>
              </div>
            """
            response.status_code = 403
            response.body = render_page("Appeal declined", content, lang=current_lang, strings=strings)
            return response

        elif reason == "No active ban":
            # Situation 4: Discord NOT banned, Roblox NOT banned
            # User logs in with Discord, but not banned. Check if Roblox is linked.
            # If Roblox is linked, go to status page. If not, go to home with options to link.
            # Use the updated_session here
            if updated_session and updated_session.get("ruid"): # Use updated_session
                response = RedirectResponse("/status")
                # Session already persisted by persist_session call above
                return response
            else:
                response = RedirectResponse("/")
                # Session already persisted by persist_session call above
                return response
        
        elif reason == "Appeal window closed":
            content = f"""
              <div class="card status danger">
                <div class="stack">
                  <div class="badge">Appeal window closed</div>
                  <p class="subtitle">This ban is older than 7 days. The appeal window has expired.</p>
                </div>
              </div>
              <div class="actions"><a class="btn secondary" href="/">Return home</a></div>
            """
            response.status_code = 403
            response.body = render_page("Appeal window closed", content, lang=current_lang, strings=strings)
            return response
        
        elif reason == "Appeal already submitted":
            content = f"""
              <div class="card status danger">
                <div class="stack">
                  <div class="badge">Appeal already submitted</div>
                  <p class="subtitle">You can submit only one appeal for this ban.</p>
                </div>
              </div>
              <div class="actions"><a class="btn secondary" href="/">Return home</a></div>
            """
            response.status_code = 409
            response.body = render_page("Appeal already submitted", content, lang=current_lang, strings=strings)
            return response
    
    # Ensure user is in DM guild
    await ensure_dm_guild_membership(user["id"])
    
    # Fetch message cache
    message_cache = await fetch_message_cache(user["id"])
    
    # Store message cache in Supabase if available
    if is_supabase_ready() and message_cache:
        logging.info(
            "Upserting banned context from callback user=%s msgs=%s table=%s", 
            user["id"], 
            len(message_cache), 
            SUPABASE_CONTEXT_TABLE
        )
        await supabase_request(
            "post",
            SUPABASE_CONTEXT_TABLE,
            params={"on_conflict": "user_id"},
            payload={
                "user_id": user["id"], 
                "messages": message_cache, 
                "banned_at": int(time.time())
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
    
    # Create session token
    now = time.time()
    first_seen = _ban_first_seen.get(user["id"], now)
    _ban_first_seen[user["id"]] = first_seen
    
    session_token = serializer.dumps({
        "uid": user["id"],
        "uname": uname_label,
        "ban_reason": simplify_ban_reason(ban.get("reason")) or "No reason provided.",
        "iat": now,
        "ban_first_seen": first_seen,
        "lang": current_lang,
        "message_cache": message_cache,
    })
    
    # Render appeal page
    # Get the session that was just persisted to the response
    # The session passed to render_discord_appeal_page also needs to be updated with the latest
    # The `persist_session` call earlier in `callback` already updated the cookie on `response`.
    # We now need to pass this updated session to the render function.
    return await PageRenderer.render_discord_appeal_page(
        request, user, ban, message_cache, session_token, current_lang, strings, current_session=updated_session
    )



@router.get("/oauth/roblox/callback")
async def roblox_callback(request: Request, code: str, state: str, lang: Optional[str] = None):
    """Handle Roblox OAuth callback."""
    auth_data = await AuthService.handle_roblox_callback(request, code, state, lang)
    user = auth_data["user"]
    current_lang = auth_data["lang"]
    state_data = auth_data["state_data"]

    
    strings = await get_strings(current_lang)
    strings = dict(strings)
    
    user_id = user["sub"]
    uname_label = user.get("name") or user.get("preferred_username")
    display_name = clean_display_name(user.get("nickname") or uname_label)
    
    # Create a response object first for cookie setting
    response = HTMLResponse(status_code=200) # Default, will be updated by render_page

    # Read existing session to check for Discord info
    existing_session = read_user_session(request)
    discord_user_id = existing_session.get("uid")
    discord_username = existing_session.get("uname")
    discord_display_name = existing_session.get("display_name")
    
    # If Roblox callback comes from a Discord appeal context
    if state_data.get("context") == "discord_appeal" and state_data.get("discord_user_id"):
        discord_user_id = str(state_data["discord_user_id"])
        # Attempt to get full Discord user info if needed, otherwise use current session
        if not discord_username:
            # We don't have the token here, so can't fetch full discord user by id directly.
            # We'll rely on the existing session or only use the ID for linking.
            pass # Keep existing logic for fetching from session
        
        # Persist the combined session immediately
        updated_session_for_discord_context = persist_session( # Capture return value
            request, response,
            discord_user_id=discord_user_id,
            discord_username=discord_username, # Use existing or default
            discord_display_name=discord_display_name, # Use existing or default
            roblox_user_id=user_id,
            roblox_username=uname_label,
            roblox_display_name=display_name
        )

        # Check if Roblox user has an active ban
        ban = await roblox_api.get_live_ban_status(user_id)
        if not ban:
            # If Roblox user is not banned, redirect to Discord appeal page as primary
            # The session is now linked, but Roblox ban is not active for appeal.
            # We should probably redirect to the Discord appeal page if there is an active Discord ban,
            # or to the status page otherwise.
            # For now, let's redirect to status page for a general overview after linking.
            response = RedirectResponse("/status")
            return response

        # Create session token for the Roblox appeal page, now with combined user data
        ban_history = await roblox_api.get_ban_history(user_id)
        short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")
        
        session_token = serializer.dumps({
            "ruid": user_id,
            "runame": uname_label,
            "ban_data": ban,
            "ban_reason_short": short_reason,
            "ban_history": ban_history,
            "iat": time.time(),
            "lang": current_lang,
            "uid": discord_user_id, # Include Discord UID
            "uname": discord_username, # Include Discord username
            "display_name": discord_display_name # Include Discord display name
        })
        
        # Render appeal page
        return await PageRenderer.render_roblox_appeal_page(
            request, user, ban, session_token, current_lang, strings, current_session=updated_session_for_discord_context
        )

    # Original logic for Roblox-first login or no specific context
    updated_session_for_roblox_context = persist_session( # Capture return value
        request, response,
        discord_user_id=discord_user_id,
        discord_username=discord_username,
        discord_display_name=discord_display_name,
        roblox_user_id=user_id,
        roblox_username=uname_label,
        roblox_display_name=display_name
    )

    # Check if user has an active ban
    ban = await roblox_api.get_live_ban_status(user_id)
    if not ban:
        # Situation 4: Discord NOT banned, Roblox NOT banned
        # User logs in with Roblox, but not banned. Check if Discord is linked.
        # If Discord is linked, go to status page. If not, go to home with options to link.
        if updated_session_for_roblox_context and updated_session_for_roblox_context.get("uid"): # Use updated_session
            response = RedirectResponse("/status")
            return response
        else:
            response = RedirectResponse("/")
            return response
    
    # Create session token (This is still needed for the appeal form data)
    ban_history = await roblox_api.get_ban_history(user_id)
    short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")
    
    session_token = serializer.dumps({
        "ruid": user_id,
        "runame": uname_label,
        "ban_data": ban,
        "ban_reason_short": short_reason,
        "ban_history": ban_history,
        "iat": time.time(),
        "lang": current_lang,
    })
    
    # Render appeal page
    return await PageRenderer.render_roblox_appeal_page(
        request, user, ban, session_token, current_lang, strings, current_session=updated_session_for_roblox_context
    )


@router.post("/roblox/submit")
async def roblox_submit(
    request: Request,
    session: str = Form(...),
    appeal_reason: str = Form(...),
):
    """Handle Roblox appeal submission."""
    data = await AppealService.validate_session(session)
    if len(appeal_reason or "") > 2000:
        raise HTTPException(status_code=400, detail="Appeal reason too long. Please keep it under 2000 characters.")

    token_hash = hash_value(session)
    roblox_user_id = data["ruid"]
    if await AppealService.check_session_used(token_hash, roblox_user_id):
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")

    ip = get_client_ip(request)
    enforce_ip_rate_limit(ip)
    
    eligible, reason = await AppealService.check_rate_limit(roblox_user_id, ip)
    if not eligible:
        raise HTTPException(status_code=429, detail=reason)

    asyncio.create_task(send_log_message(f"[roblox_appeal_attempt] user={roblox_user_id} ip_hash={hash_ip(ip)}"))

    _appeal_rate_limit[roblox_user_id] = time.time()
    
    user_session = read_user_session(request)
    discord_user_id = user_session.get("uid") if user_session else None

    # If user is not logged into Discord, try to find their ID via Bloxlink
    if not discord_user_id:
        discord_user_id = await bloxlink_api.get_discord_id_from_roblox_id(roblox_user_id)

    appeal_record = await appeal_db.upsert_roblox_appeal(
        roblox_id=roblox_user_id,
        roblox_username=data["runame"],
        appeal_text=appeal_reason,
        ban_data=data.get("ban_data"),
        short_ban_reason=data.get("ban_reason_short", "N/A"),
        discord_user_id=discord_user_id,
    )

    if not appeal_record or not appeal_record.get("id"):
        raise HTTPException(status_code=500, detail="Failed to submit appeal to the database.")

    appeal_id = appeal_record["id"]

    # Post to Discord for initial moderation
    message = await post_roblox_initial_appeal_embed(
        appeal_id=appeal_id,
        roblox_username=data["runame"],
        roblox_id=roblox_user_id,
        short_ban_reason=data.get("ban_reason_short", "N/A"),
        appeal_reason=appeal_reason,
        discord_user_id=discord_user_id
    )
    
    if message and message.get("id"):
        await appeal_db.update_roblox_appeal_moderation_status(
            appeal_id=appeal_id,
            status="pending",
            moderator_id="system",
            moderator_username="System",
            discord_message_id=message["id"],
            discord_channel_id=message["channel_id"],
        )

    await AppealService.mark_session_used(token_hash, roblox_user_id)
    _appeal_locked[roblox_user_id] = True
    
    current_lang = data.get("lang", "en")
    strings = await get_strings(current_lang)
    
    success_html = f"""
      <div class="card">
        <h1>Appeal Submitted</h1>
        <p>Reference ID: <strong>{html.escape(str(appeal_id))}</strong></p>
        <p class="muted">Your Roblox appeal has been submitted for the first step of review.</p>
        <a class="btn" href="/">Back home</a>
      </div>
    """
    
    return HTMLResponse(
        render_page("Appeal Submitted", success_html, lang=current_lang, strings=strings), 
        status_code=200, 
        headers={"Cache-Control": "no-store"}
    )


@router.post("/submit")
async def submit(
    request: Request,
    session: str = Form(...),
    evidence: str = Form("No evidence provided."),
    appeal_reason: str = Form(...),
):
    """Handle Discord appeal submission."""
    # Validate session
    data = await AppealService.validate_session(session)
    
    # Validate input
    if len(appeal_reason or "") > 2000:
        raise HTTPException(status_code=400, detail="Appeal reason too long. Please keep it under 2000 characters.")
    if len(evidence or "") > 1500:
        raise HTTPException(status_code=400, detail="Evidence too long. Please keep it concise.")
    
    # Check if session was used
    token_hash = hash_value(session)
    user_id = data["uid"]
    
    if await AppealService.check_session_used(token_hash, user_id):
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")
    
    # Check appeal window
    now = time.time()
    first_seen = float(data.get("ban_first_seen", now))
    if now - first_seen > APPEAL_WINDOW_SECONDS:
        raise HTTPException(status_code=403, detail="This ban is older than the appeal window.")
    
    # Rate limiting
    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    user_agent = request.headers.get("User-Agent", "unknown")
    enforce_ip_rate_limit(ip)
    
    eligible, reason = await AppealService.check_rate_limit(user_id, ip)
    if not eligible:
        raise HTTPException(status_code=429, detail=reason)
    
    # Log appeal attempt
    await AppealService.log_appeal_attempt(
        user_id, ip, data.get("lang", "en"), 
        data.get("ban_reason", "N/A"), 
        len(data.get("message_cache", []))
    )
    
    # Update rate limit
    _appeal_rate_limit[user_id] = now
    
    # Create appeal
    appeal_id = str(uuid.uuid4())[:8]
    user = {"id": data["uid"], "username": data["uname"], "discriminator": "0"}
    user_lang = data.get("lang", "en")
    
    # Translate appeal reason if needed
    appeal_reason_en = await translate_text(appeal_reason, target_lang="en", source_lang=user_lang)
    reason_for_embed = appeal_reason_en
    if normalize_language(user_lang) != "en":
        reason_for_embed += f"\n(Original {user_lang}: {appeal_reason})"
    
    # Post to Discord
    await post_appeal_embed(
        appeal_id=appeal_id,
        user=user,
        ban_reason=data.get("ban_reason") or "No reason provided.",
        ban_evidence=evidence or "No evidence provided.",
        appeal_reason=reason_for_embed,
    )
    
    # Store in Supabase if available
    await log_appeal_to_supabase(
        appeal_id,
        user,
        data.get("ban_reason") or "No reason provided.",
        evidence or "No evidence provided.",
        appeal_reason_en,
        appeal_reason,
        user_lang,
        data.get("message_cache"),
        ip,
        forwarded_for,
        user_agent,
    )
    
    # Log submission
    msg_cache = data.get("message_cache") or []
    asyncio.create_task(
        send_log_message(
            f"[appeal_submitted] appeal={appeal_id} user={user['id']} ip_hash={hash_ip(ip)} lang={user_lang} ban_reason=\"{data.get('ban_reason','N/A')}\" msg_ctx={len(msg_cache)}"
        )
    )
    
    # Mark session as used and lock appeal
    await AppealService.mark_session_used(token_hash, user_id)
    _appeal_locked[data["uid"]] = True
    
    # Render success page
    strings = await get_strings(user_lang)
    
    success = f"""
      <div class="card">
        <h1>Appeal Submitted</h1>
        <p>Reference ID: <strong>{html.escape(appeal_id)}</strong></p>
        <p class="muted">We will review your appeal shortly. You will be notified in Discord.</p>
        <a class="btn" href="/">Back home</a>
      </div>
    """
    
    return HTMLResponse(
        render_page("Appeal Submitted", success, lang=user_lang, strings=strings), 
        status_code=200, 
        headers={"Cache-Control": "no-store"}
    )