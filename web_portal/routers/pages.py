from __future__ import annotations

import asyncio
import html
import json
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
from ..services import appeal_db, roblox_api
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
    update_session_with_platform,
)
from ..services.supabase import (
    get_remote_last_submit,
    is_session_token_used,
    is_supabase_ready,
    mark_session_token,
    resolve_internal_user_id,
    supabase_request,
)
from ..services.supabase import fetch_appeal_history, log_appeal_to_supabase
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

def _render_appeal_ineligible(reason: str, user_label: str, strings: Dict[str, str], current_lang: str):
    """Return the appropriate response for ineligible appeal reasons."""
    name = html.escape(user_label or "You")
    if reason == "Appeal declined":
        content = f"""
          <div class="card status danger">
            <h1 style="margin-bottom:10px;">Appeal declined</h1>
            <p>{name}, your previous appeal was declined. Further appeals are blocked.</p>
            <a class="btn" href="/">Return home</a>
          </div>
        """
        return HTMLResponse(render_page("Appeal declined", content, lang=current_lang, strings=strings), status_code=403, headers={"Cache-Control": "no-store"})

    if reason == "No active ban":
        return RedirectResponse("/")

    if reason == "Appeal window closed":
        content = """
          <div class="card status danger">
            <div class="stack">
              <div class="badge">Appeal window closed</div>
              <p class="subtitle">This ban is older than 7 days. The appeal window has expired.</p>
            </div>
          </div>
          <div class="actions"><a class="btn secondary" href="/">Return home</a></div>
        """
        return HTMLResponse(render_page("Appeal window closed", content, lang=current_lang, strings=strings), status_code=403, headers={"Cache-Control": "no-store"})

    if reason == "Appeal already submitted":
        content = """
          <div class="card status danger">
            <div class="stack">
              <div class="badge">Appeal already submitted</div>
              <p class="subtitle">You can submit only one appeal for this ban.</p>
            </div>
          </div>
          <div class="actions"><a class="btn secondary" href="/">Return home</a></div>
        """
        return HTMLResponse(render_page("Appeal already submitted", content, lang=current_lang, strings=strings), status_code=409, headers={"Cache-Control": "no-store"})

    return RedirectResponse("/")


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
            render_page("BlockSpin Appeals", content, lang=current_lang, strings=strings),
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
        is_logged_in = "internal_user_id" in session
        has_discord = bool(session.get("uid"))
        has_roblox = bool(session.get("ruid"))

        # Action buttons in the hero section
        hero_actions = []
        if is_logged_in:
            hero_actions.append('<a class="btn btn--primary" href="/status">Check Appeal Status</a>')
        else:
            # Not logged in, show both options for initial login
            hero_actions.append('<a class="btn btn--primary" href="/status">Check Appeal Status</a>')
            hero_actions.append('<a class="btn btn--soft" href="#how-it-works">Learn how it works</a>')
        
        # Action buttons in the side panel
        panel_actions = []
        if is_logged_in:
            panel_actions.append('<p class="muted">You are signed in.</p>')
        else: # Not logged in, show guidance
            panel_actions.append('<p class="muted">Use the buttons in the header to authenticate with Discord or Roblox.</p>')
            panel_actions.append('<a class="btn btn--soft btn--wide" href="/status">Preview appeal status</a>')

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
            <div class="panel" id="how-it-works">
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
        <script id="home-link-data" type="application/json">{html.escape(json.dumps({
            "needs_discord": bool(has_roblox and not has_discord),
            "needs_roblox": bool(has_discord and not has_roblox),
            "discord_url": discord_login_url or "",
            "roblox_url": roblox_login_url or "",
            "session_active": bool(is_logged_in),
        }))}</script>

        <section class="grid">
          <article class="card">
            <div class="card__top">
              <h2 class="card__title">Appeal history</h2>
              <div class="chip" id="historyChip">Loading data…</div>
            </div>

            <div class="callout callout--info" id="home-link-prompt" style="margin-bottom:16px; display:none;">
              <span class="muted small">Connect both platforms to see the complete appeal timeline.</span>
              <div id="home-link-buttons" style="margin-top:6px; display:flex; gap:6px;"></div>
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
            linkPrompt: document.getElementById("home-link-prompt"),
            linkButtons: document.getElementById("home-link-buttons"),
            linkData: document.getElementById("home-link-data"),
          };

          function esc(s){
            const escMap = {
              "&":"&amp;",
              "<":"&lt;",
              ">":"&gt;",
              '"':"&quot;",
              "'":"&#39;",
              "/":"&#x2F;"
            };
            return String(s ?? "").replace(/[&<>"'/]/g, ch => escMap[ch]);
          }

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

          let linkInfo = {};
          if (els.linkData){
            try{
              linkInfo = JSON.parse(els.linkData.textContent || "{}");
            }catch(err){
              linkInfo = {};
            }
          }

          function refreshLinkPrompt(){
            if (!els.linkPrompt || !els.linkButtons){
              return;
            }
            const needsDiscord = linkInfo.needs_discord && linkInfo.discord_url;
            const needsRoblox = linkInfo.needs_roblox && linkInfo.roblox_url;
            let html = "";
            if (needsDiscord){
              html += `<a class="btn btn--discord btn--soft btn--wide" href="${esc(linkInfo.discord_url)}" target="_blank" rel="noopener noreferrer">Link Discord</a>`;
            }
            if (needsRoblox){
              html += `<a class="btn btn--roblox btn--soft btn--wide" href="${esc(linkInfo.roblox_url)}" target="_blank" rel="noopener noreferrer">Link Roblox</a>`;
            }
            if (html){
              els.linkButtons.innerHTML = html;
              els.linkPrompt.style.display = "flex";
            } else {
              els.linkPrompt.style.display = "none";
            }
          }

          function setEmptyState(){
            if (els.liveStatus) els.liveStatus.textContent = linkInfo.session_active ? "Signed in" : "Sign in to see your status";
            if (els.liveRef) els.liveRef.textContent = "—";
            if (els.liveDecision) els.liveDecision.textContent = "—";
            if (els.chip) els.chip.textContent = "No appeals yet";
            if (els.list){
              els.list.hidden = true;
              els.list.innerHTML = "";
            }
            if (els.empty){
              els.empty.hidden = false;
              const needs = [];
              if (linkInfo.needs_discord) needs.push("Discord");
              if (linkInfo.needs_roblox) needs.push("Roblox");
              const emptyText = needs.length
                ? `Connect ${needs.join(" and ")} to view your latest appeals.`
                : linkInfo.session_active
                  ? "You are signed in but have no appeals yet."
                  : "Sign in to see your history and live status.";
              els.empty.textContent = emptyText;
            }
          }

          function setListState(history){
            if (!els.list){
              return;
            }
            els.list.hidden = history.length === 0;
            if (!history.length){
              els.list.innerHTML = "";
              return;
            }
            els.list.innerHTML = history.map(item => {
              const s = statusLabel(item.status);
              const cls = statusClass(item.status);
              const ref = esc(item.appeal_id || "—");
              const submitted = esc(item.created_at || "—");
              const reason = esc(item.ban_reason || "No reason recorded");
              const platform = item.platform ? `<span class="chip chip--ghost">${esc(item.platform)}</span>` : "";
              return `
                <li class="row">
                  <div class="row__left">
                    <div class="pill pill--${cls}">${esc(s)}</div>
                    <div class="row__meta">
                      <div class="row__k">Reference</div>
                      <div class="row__v">${ref} ${platform}</div>
                    </div>
                    <div class="row__meta">
                      <div class="row__k">Submitted</div>
                      <div class="row__v">${submitted}</div>
                    </div>
                  </div>
                  <div class="row__right">
                    <div class="row__k">Ban reason</div>
                    <div class="row__v row__v--wrap">${reason}</div>
                  </div>
                </li>
              `;
            }).join("");
          }

          function updateLiveMetrics(latest, count){
            if (els.liveStatus) els.liveStatus.textContent = "Active";
            if (els.liveRef) els.liveRef.textContent = latest?.appeal_id ? String(latest.appeal_id) : "—";
            if (els.liveDecision) els.liveDecision.textContent = statusLabel(latest?.status);
            if (els.chip) els.chip.textContent = `${count} appeal${count === 1 ? "" : "s"}`;
          }

          function updateSignal(online){
            if (els.feedState) els.feedState.textContent = online ? "Connected" : "Disconnected";
            if (!els.signalChip){
              return;
            }
            els.signalChip.textContent = online ? "Portal online" : "Live updates unavailable";
            els.signalChip.classList.toggle("chip--ok", online);
            els.signalChip.classList.toggle("chip--warn", !online);
          }

          async function tick(){
            try{
              const r = await fetch("/status/data", { headers: { "Accept":"application/json" } });
              if (!r.ok){
                throw new Error("status request failed");
              }
              const data = await r.json();
              const hist = Array.isArray(data.history) ? data.history : [];
              if (!hist.length){
                setEmptyState();
                updateSignal(true);
                return;
              }
              updateLiveMetrics(hist[0], hist.length);
              setListState(hist);
              if (els.empty) els.empty.hidden = true;
              updateSignal(true);
            }catch(err){
              setEmptyState();
              if (els.chip) els.chip.textContent = "Live data offline";
              if (els.liveStatus) els.liveStatus.textContent = "Unavailable";
              updateSignal(false);
            }
          }

          refreshLinkPrompt();
          setEmptyState();
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
        
        internal_user_id = session.get("internal_user_id")
        if not internal_user_id:
            # Should not happen if session is valid, but as a safeguard
            raise HTTPException(status_code=401, detail="Internal user ID not found in session.")

        if is_supabase_ready():
            history = await fetch_appeal_history(internal_user_id, limit=10) # Pass internal_user_id
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
        current_session: Optional[Dict[str, Any]] = None, # Added parameter
        roblox_login_url: Optional[str] = None,
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
                const days = Math.floor((total % 86400) / 3600);
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
        
        content = f"""
          <div class="grid-2">
            <div class="form-card">
              <div class="badge">Window remaining: <span id="appealWindowRemaining" data-expires="{int(window_expires_at)}"></span></div>
              <h2 style="margin:8px 0;">Appeal your BlockSpin ban</h2>
              <p class="muted">One appeal per ban. Include context, evidence, and what you will change.</p>
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

              {roblox_login_prompt}

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
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp
    
    @staticmethod
    async def render_roblox_appeal_page(
        request: Request, 
        user: Dict[str, Any], 
        ban: Dict[str, Any], 
        session_token: str,
        current_lang: str,
        strings: Dict[str, str],
        current_session: Optional[Dict[str, Any]] = None,
        discord_login_url: Optional[str] = None,
    ) -> HTMLResponse:
        """Render the Roblox appeal page."""
        user_id = user["sub"]
        uname_label = user.get("name") or user.get("preferred_username")
        display_name = clean_display_name(user.get("nickname") or uname_label)
        
        ban_history = await roblox_api.get_ban_history(user_id)
        short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")
        
        ban_reason = html.escape(short_reason)
        user_id_label = html.escape(str(user_id))

        login_prompt = ""
        if discord_login_url:
            prompt_text = strings.get("link_discord_prompt", "Connect your Discord to receive updates about this appeal.")
            prompt_cta = strings.get("link_discord_cta", "Connect Discord")
            login_prompt = f"""
              <div class="callout callout--info" style="margin-bottom:16px;text-align:center;">
                <p class="muted" style="margin-bottom:8px;">{html.escape(prompt_text)}</p>
                <a class="btn btn--discord btn--wide" href="{html.escape(discord_login_url)}" target="_blank" rel="noopener noreferrer">{html.escape(prompt_cta)}</a>
              </div>
            """

        content = f"""
          <div class="grid-2">
            <div class="form-card">
              <h2 style="margin:8px 0;">Appeal your Roblox Ban</h2>
              <p class="muted">One appeal per ban. Be clear and concise.</p>
              <form class="form" action="/roblox/submit" method="post">
                <input type="hidden" name="session" value="{html.escape(session_token)}" />
                {login_prompt}
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
        # Note: The session is persisted in the main callback handler, not here.
        # persist_roblox_user_session(request, resp, user_id, uname_label, display_name=display_name) # Removed
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

    history_html = """
    <div class="history-wrapper">
      <div class="status-heading">
        <div>
          <div class="status-chip" id="liveStatusChip">Loading…</div>
          <p class="muted small">Latest ref: <span id="liveRef">—</span></p>
          <p class="muted small">Decision: <span id="liveDecision">Pending</span></p>
        </div>
        <div class="muted small" id="historyCount">0 appeals</div>
      </div>

      <div id="history-empty" class="muted" style="margin-bottom:10px;">Loading history…</div>
      <ul class="history-list" id="history-list" hidden></ul>
    </div>

    """

    content = f"""
      <div class="card status-card">
        <div class="status-heading">
          <h1>Appeal history for {display_name}</h1>
          <p class="muted">Monitor decisions and peer context for this ban review.</p>
        </div>
        {history_html}
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

    internal_user_id = session.get("internal_user_id")
    if not internal_user_id:
        return {"history": []} # Should not happen if session is valid
    
    all_appeals = []

    # Fetch Discord-specific appeals using internal_user_id
    discord_appeals = await fetch_appeal_history(internal_user_id, limit=25)
    for item in discord_appeals:
        item['platform'] = 'Discord'
        all_appeals.append(item)

    # Fetch Roblox appeals using internal_user_id
    roblox_appeals = await appeal_db.get_roblox_appeal_history(internal_user_id, limit=50)
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
    existing_session = read_user_session(request)
    auth_data = await AuthService.handle_discord_callback(request, code, state, lang)
    user = auth_data["user"]
    current_lang = auth_data["lang"]
    strings = await get_strings(current_lang)
    strings = dict(strings)
    state_data = auth_data.get("state_data", {})
    return_to = state_data.get("return_to")
    ip = auth_data["ip"]

    uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
    display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)

    # Account Linking Flow
    if existing_session and existing_session.get("internal_user_id"):
        response = RedirectResponse(return_to or "/status")
        internal_user_id = await resolve_internal_user_id(
            discord_id=user["id"],
            roblox_id=existing_session.get("ruid"),
            current_id=existing_session["internal_user_id"],
        )

        update_session_with_platform(
            response,
            existing_session,
            "discord",
            user["id"],
            uname_label,
            display_name,
            internal_user_id=internal_user_id,
        )
        return response

    # Standard Login/Appeal Flow
    internal_user_id = await resolve_internal_user_id(discord_id=user["id"])

    ban = await fetch_ban_if_exists(user["id"])
    
    if not ban:
        response = RedirectResponse(return_to or "/")
        persist_session(
            response,
            internal_user_id=internal_user_id,
            platform_type="discord",
            platform_id=user["id"],
            username=uname_label,
            display_name=display_name,
        )
        return response

    # --- User is Banned: Proceed with appeal flow ---
    response = HTMLResponse(status_code=200)
    updated_session = persist_session(
        response,
        internal_user_id,
        "discord",
        user["id"],
        uname_label,
        display_name,
    )

    eligible, reason = await AppealService.check_appeal_eligibility(internal_user_id, ban)
    if not eligible:
        return _render_appeal_ineligible(reason, user["username"], strings, current_lang)

    await ensure_dm_guild_membership(user["id"])
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
                "user_id": user["id"], # Store with Discord ID for context tracking
                "messages": message_cache, 
                "banned_at": int(time.time())
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
    
    # Create session token
    now = time.time()
    first_seen = _ban_first_seen.get(internal_user_id, now) # Use internal_user_id for first_seen
    _ban_first_seen[internal_user_id] = first_seen

    session_token = serializer.dumps({
        "internal_user_id": internal_user_id,
        "uid": user["id"],
        "uname": uname_label,
        "ban_reason": simplify_ban_reason(ban.get("reason")) or "No reason provided.",
        "iat": time.time(),
        "ban_first_seen": _ban_first_seen.get(internal_user_id, time.time()),
        "lang": current_lang,
        "message_cache": message_cache,
    })
    
    roblox_login_url = None
    if not updated_session.get("ruid"):
        roblox_link_state = serializer.dumps({
            "nonce": secrets.token_urlsafe(8),
            "lang": current_lang,
            "state_id": issue_state_token(ip),
            "return_to": f"/discord/resume?lang={current_lang}",
        })
        roblox_login_url = roblox_api.oauth_authorize_url(roblox_link_state)

    return await PageRenderer.render_discord_appeal_page(
        request,
        user,
        ban,
        message_cache,
        session_token,
        current_lang,
        strings,
        current_session=updated_session,
        roblox_login_url=roblox_login_url,
    )



@router.get("/oauth/roblox/callback")
async def roblox_callback(request: Request, code: str, state: str, lang: Optional[str] = None):
    """Handle Roblox OAuth callback."""
    existing_session = read_user_session(request)
    auth_data = await AuthService.handle_roblox_callback(request, code, state, lang)
    user = auth_data["user"]
    current_lang = auth_data["lang"]
    strings = await get_strings(current_lang)
    state_data = auth_data.get("state_data", {})
    user_id = user["sub"]
    uname_label = user.get("name") or user.get("preferred_username")
    display_name = clean_display_name(user.get("nickname") or uname_label)

    # Account Linking Flow
    return_to = state_data.get("return_to")
    if existing_session and existing_session.get("internal_user_id"):
        response = RedirectResponse(return_to or "/status")
        internal_user_id = await resolve_internal_user_id(
            discord_id=existing_session.get("uid"),
            roblox_id=user_id,
            current_id=existing_session["internal_user_id"],
        )

        update_session_with_platform(
            response,
            existing_session,
            "roblox",
            user_id,
            uname_label,
            display_name,
            internal_user_id=internal_user_id,
        )
        return response

    # Standard Login/Appeal Flow
    internal_user_id = await resolve_internal_user_id(roblox_id=user_id)
    
    ban = await roblox_api.get_live_ban_status(user_id)
    if not ban:
        response = RedirectResponse(return_to or "/")
        persist_session(
            response,
            internal_user_id,
            "roblox",
            user_id,
            uname_label,
            display_name,
        )
        return response

    # --- User is Banned: Proceed with appeal flow ---
    response = HTMLResponse(status_code=200)
    updated_session_for_roblox_context = persist_session(
        response,
        internal_user_id,
        "roblox",
        user_id,
        uname_label,
        display_name,
    )
    
    ban_history = await roblox_api.get_ban_history(user_id)
    short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")
    
    session_token = serializer.dumps({
        "internal_user_id": internal_user_id,
        "ruid": user_id,
        "runame": uname_label,
        "ban_data": ban,
        "ban_reason_short": short_reason,
        "ban_history": ban_history,
        "iat": time.time(),
        "lang": current_lang,
    })
    link_state = serializer.dumps({
        "nonce": secrets.token_urlsafe(8),
        "lang": current_lang,
        "state_id": issue_state_token(auth_data["ip"]),
        "return_to": f"/roblox/resume?lang={current_lang}",
    })
    discord_login_url = None
    if not updated_session_for_roblox_context.get("uid"):
        discord_login_url = discord_oauth_authorize_url(link_state)
    
    return await PageRenderer.render_roblox_appeal_page(
        request,
        user,
        ban,
        session_token,
        current_lang,
        strings,
        current_session=updated_session_for_roblox_context,
        discord_login_url=discord_login_url,
    )


@router.get("/roblox/resume", response_class=HTMLResponse)
async def roblox_resume(request: Request, lang: Optional[str] = None):
    """Return to the Roblox appeal form after linking Discord."""
    current_lang = await detect_language(request, lang)
    strings = await get_strings(current_lang)
    session = read_user_session(request)
    if not session or not session.get("ruid"):
        return RedirectResponse("/")

    internal_user_id = session.get("internal_user_id")
    if not internal_user_id:
        return RedirectResponse("/status")

    user_id = session["ruid"]
    uname_label = session.get("runame") or ""
    display_name = clean_display_name(session.get("display_name") or uname_label)
    ban = await roblox_api.get_live_ban_status(user_id)
    if not ban:
        return RedirectResponse("/")

    eligible, reason = await AppealService.check_appeal_eligibility(internal_user_id, ban)
    if not eligible:
        return _render_appeal_ineligible(reason, display_name or uname_label or "You", strings, current_lang)

    ban_history = await roblox_api.get_ban_history(user_id)
    short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")
    session_token = serializer.dumps({
        "internal_user_id": internal_user_id,
        "ruid": user_id,
        "runame": uname_label,
        "ban_data": ban,
        "ban_reason_short": short_reason,
        "ban_history": ban_history,
        "iat": time.time(),
        "lang": current_lang,
    })

    link_state = serializer.dumps({
        "nonce": secrets.token_urlsafe(8),
        "lang": current_lang,
        "state_id": issue_state_token(get_client_ip(request)),
        "return_to": f"/roblox/resume?lang={current_lang}",
    })
    discord_login_url = None
    if not session.get("uid"):
        discord_login_url = discord_oauth_authorize_url(link_state)

    user_info = {
        "sub": user_id,
        "name": session.get("runame") or "",
        "preferred_username": session.get("runame") or "",
        "nickname": session.get("display_name") or session.get("runame"),
    }

    return await PageRenderer.render_roblox_appeal_page(
        request,
        user_info,
        ban,
        session_token,
        current_lang,
        strings,
        current_session=session,
        discord_login_url=discord_login_url,
    )


@router.get("/discord/resume", response_class=HTMLResponse)
async def discord_resume(request: Request, lang: Optional[str] = None):
    """Return to the Discord appeal form after linking Roblox."""
    current_lang = await detect_language(request, lang)
    strings = await get_strings(current_lang)
    session = read_user_session(request)
    if not session or not session.get("uid"):
        return RedirectResponse("/")

    internal_user_id = session.get("internal_user_id")
    if not internal_user_id:
        return RedirectResponse("/status")

    user_id = session["uid"]
    ban = await fetch_ban_if_exists(user_id)
    if not ban:
        return RedirectResponse("/")

    eligible, reason = await AppealService.check_appeal_eligibility(internal_user_id, ban)
    if not eligible:
        user_label = session.get("uname") or session.get("display_name") or "You"
        return _render_appeal_ineligible(reason, user_label, strings, current_lang)

    await ensure_dm_guild_membership(user_id)
    message_cache = await fetch_message_cache(user_id)

    now = time.time()
    first_seen = _ban_first_seen.get(internal_user_id, now)
    _ban_first_seen[internal_user_id] = first_seen

    session_token = serializer.dumps({
        "internal_user_id": internal_user_id,
        "uid": user_id,
        "uname": session.get("uname"),
        "ban_reason": simplify_ban_reason(ban.get("reason")) or "No reason provided.",
        "iat": time.time(),
        "ban_first_seen": first_seen,
        "lang": current_lang,
        "message_cache": message_cache,
    })

    roblox_login_url = None
    if not session.get("ruid"):
        link_state = serializer.dumps({
            "nonce": secrets.token_urlsafe(8),
            "lang": current_lang,
            "state_id": issue_state_token(get_client_ip(request)),
            "return_to": f"/discord/resume?lang={current_lang}",
        })
        roblox_login_url = roblox_api.oauth_authorize_url(link_state)

    user = {
        "id": user_id,
        "username": session.get("uname") or "",
        "discriminator": "0",
        "global_name": session.get("display_name") or session.get("uname"),
    }

    return await PageRenderer.render_discord_appeal_page(
        request,
        user,
        ban,
        message_cache,
        session_token,
        current_lang,
        strings,
        current_session=session,
        roblox_login_url=roblox_login_url,
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
    internal_user_id = data.get("internal_user_id") # Retrieve internal_user_id from session data

    if not internal_user_id:
        raise HTTPException(status_code=400, detail="Internal user ID not found in session.")

    if await AppealService.check_session_used(token_hash, roblox_user_id):
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")

    ip = get_client_ip(request)
    enforce_ip_rate_limit(ip)
    
    eligible, reason = await AppealService.check_rate_limit(internal_user_id, ip) # Use internal_user_id for rate limit
    if not eligible:
        raise HTTPException(status_code=429, detail=reason)

    asyncio.create_task(send_log_message(f"[roblox_appeal_attempt] user={roblox_user_id} ip_hash={hash_ip(ip)}"))

    _appeal_rate_limit[internal_user_id] = time.time() # Use internal_user_id for rate limit

    # discord_user_id will be handled by the internal user record in the database
    # No need to call bloxlink_api.get_discord_id_from_roblox_id here anymore

    appeal_record = await appeal_db.upsert_roblox_appeal(
        internal_user_id=internal_user_id, # Pass internal_user_id
        roblox_id=roblox_user_id,
        roblox_username=data["runame"],
        appeal_text=appeal_reason,
        ban_data=data.get("ban_data"),
        short_ban_reason=data.get("ban_reason_short", "N/A"),
        discord_user_id=None, # Discord ID from session is no longer reliable; use internal_user_id
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
        discord_user_id=None # Discord ID from session is no longer reliable; use internal_user_id for notification logic if needed
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
    _appeal_locked[internal_user_id] = True # Use internal_user_id for appeal locked state
    
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
    internal_user_id = data.get("internal_user_id") # Retrieve internal_user_id from session data
    if is_supabase_ready():
        await log_appeal_to_supabase(
            appeal_id,
            user,
            internal_user_id, # Pass internal_user_id
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
    _appeal_locked[internal_user_id] = True # Use internal_user_id for appeal locked state
    
    
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
