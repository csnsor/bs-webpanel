from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature

from ..i18n import detect_language, get_strings, translate_text
from ..services import roblox_api
from ..services.discord_api import (
    ensure_dm_guild_membership,
    exchange_code_for_token,
    fetch_ban_if_exists,
    fetch_discord_user,
    fetch_guild_name,
    oauth_authorize_url as discord_oauth_authorize_url,
    post_appeal_embed,
    send_log_message,
    store_user_token,
)
from ..services.message_cache import fetch_message_cache
from ..services.security import enforce_ip_rate_limit, issue_state_token, validate_state_token
from ..services.sessions import (
    maybe_persist_session,
    persist_roblox_user_session,
    persist_user_session,
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
    simplify_ban_reason,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, lang: Optional[str] = None):
    current_lang = await detect_language(request, lang)
    strings = await get_strings(current_lang)
    ip = get_client_ip(request)
    state_token = issue_state_token(ip)
    state = serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang, "state_id": state_token})
    asyncio.create_task(send_log_message(f"[visit_home] ip_hash={hash_ip(ip)} lang={current_lang}"))
    user_session = read_user_session(request)
    user_session, session_refreshed = await refresh_session_profile(user_session)
    strings = dict(strings)
    discord_login_url = discord_oauth_authorize_url(state)
    roblox_login_url = roblox_api.oauth_authorize_url(state)

    strings["top_actions"] = build_user_chip(
        user_session, discord_login_url=discord_login_url, roblox_login_url=roblox_login_url
    )

    content = f"""
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
          <a class="btn btn--primary" href="{html.escape(discord_login_url)}" aria-label="Appeal with Discord">
            Appeal with Discord
          </a>
          <a class="btn btn--soft" href="/status">
            View Status
          </a>
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
            Tip: “I understand what I did, here’s context, here’s how I’ll improve” is the fastest path.
          </div>

          <div class="panel__actions">
            <a class="btn btn--discord btn--wide" href="{html.escape(discord_login_url)}">Continue with Discord</a>
            <a class="btn btn--roblox btn--wide" href="{html.escape(roblox_login_url)}">Continue with Roblox</a>
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
          Appeals are reviewed by moderators. Decisions may be final. Don’t spam submissions.
        </div>
      </article>
    </section>
    """

    strings["script_nonce"] = secrets.token_urlsafe(12)
    strings["script_block"] = """
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

      function esc(s){ return String(s ?? "").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
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

    response = HTMLResponse(render_page("BlockSpin — Appeals", content, lang=current_lang, strings=strings), headers={"Cache-Control": "no-store"})
    maybe_persist_session(response, user_session, session_refreshed)
    response.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
    return response


@router.get("/tos", response_class=HTMLResponse)
async def tos():
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
        resp = HTMLResponse(render_page("Appeal status", content, lang=current_lang, strings=strings), status_code=401, headers={"Cache-Control": "no-store"})
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
    resp = HTMLResponse(render_page("Appeal status", content, lang=current_lang, strings=strings), headers={"Cache-Control": "no-store"})
    maybe_persist_session(resp, session, session_refreshed)
    resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@router.get("/callback")
async def callback(request: Request, code: str, state: str, lang: Optional[str] = None):
    try:
        state_data = serializer.loads(state)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid state")

    current_lang = normalize_language(lang or state_data.get("lang"))
    strings = await get_strings(current_lang)

    token = await exchange_code_for_token(code)
    user = await fetch_discord_user(token["access_token"])
    store_user_token(user["id"], token)
    uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
    display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
    ip = get_client_ip(request)
    state_id = state_data.get("state_id")
    if not validate_state_token(state_id, ip):
        raise HTTPException(status_code=400, detail="Invalid or replayed state")
    asyncio.create_task(send_log_message(f"[auth] user={user['id']} ip_hash={hash_ip(ip)} lang={current_lang}"))

    if is_supabase_ready():
        history = await fetch_appeal_history(user["id"], limit=10)
        history_html = render_history_items(history, format_timestamp=format_timestamp)
    else:
        history_html = "<div class='muted'></div>"

    strings = dict(strings)
    strings["user_chip"] = build_user_chip({"display_name": display_name, "uname": uname_label})

    def respond(body_html: str, title: str, status_code: int = 200) -> HTMLResponse:
        resp = HTMLResponse(render_page(title, body_html, lang=current_lang, strings=strings), status_code=status_code, headers={"Cache-Control": "no-store"})
        persist_user_session(resp, user["id"], uname_label, display_name=display_name)
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp

    if _declined_users.get(user["id"]):
        content = f"""
          <div class="card status danger">
            <h1 style="margin-bottom:10px;">Appeal declined</h1>
            <p>{html.escape(user['username'])}, your previous appeal was declined. Further appeals are blocked.</p>
            <a class="btn" href="/">Return home</a>
          </div>
        """
        return respond(content, "Appeal declined", 403)

    ban = await fetch_ban_if_exists(user["id"])
    if not ban:
        content = f"""
          <div class="card status">
            <p>No active ban found for {html.escape(user['username'])}#{html.escape(user.get('discriminator','0'))}.</p>
            <a class="btn" href="/">Back home</a>
          </div>
        """
        return respond(content, "No active ban", 200)

    now = time.time()
    first_seen = _ban_first_seen.get(user["id"], now)
    _ban_first_seen[user["id"]] = first_seen
    window_expires_at = first_seen + APPEAL_WINDOW_SECONDS
    already_appealed = _appeal_locked.get(user["id"], False)

    if now > window_expires_at:
        expired = f"""
          <div class="card status danger">
            <div class="stack">
              <div class="badge">Appeal window closed</div>
              <p class="subtitle">This ban is older than 7 days. The appeal window has expired.</p>
            </div>
          </div>
          <div class="actions"><a class="btn secondary" href="/">Return home</a></div>
        """
        return respond(expired, "Appeal window closed", 403)

    if already_appealed:
        blocked = f"""
          <div class="card status danger">
            <div class="stack">
              <div class="badge">Appeal already submitted</div>
              <p class="subtitle">You can submit only one appeal for this ban.</p>
            </div>
          </div>
          <div class="actions"><a class="btn secondary" href="/">Return home</a></div>
        """
        return respond(blocked, "Appeal already submitted", 409)

    await ensure_dm_guild_membership(user["id"])

    message_cache = await fetch_message_cache(user["id"])
    guild_name = await fetch_guild_name(str(TARGET_GUILD_ID))

    if is_supabase_ready() and message_cache:
        logging.info("Upserting banned context from callback user=%s msgs=%s table=%s", user["id"], len(message_cache), SUPABASE_CONTEXT_TABLE)
        await supabase_request(
            "post",
            SUPABASE_CONTEXT_TABLE,
            params={"on_conflict": "user_id"},
            payload={"user_id": user["id"], "messages": message_cache, "banned_at": int(time.time())},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    session_token = serializer.dumps(
        {
            "uid": user["id"],
            "uname": uname_label,
            "ban_reason": simplify_ban_reason(ban.get("reason")) or "No reason provided.",
            "iat": time.time(),
            "ban_first_seen": first_seen,
            "lang": current_lang,
            "message_cache": message_cache,
        }
    )
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
        message_cache_html = f"<div class='chat-box'>{''.join(rows)}</div>"
    else:
        message_cache_html = f"<div class='muted' style='padding:10px; border:1px dashed var(--border); border-radius:8px;'>{strings['no_messages']}</div>"

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

          <details class="details">
            <summary>Your history</summary>
            <div class="details-body">{history_html}</div>
          </details>
          <div class="btn-row" style="margin-top:10px;">
            <a class="btn secondary" href="/">Back home</a>
          </div>
        </div>
      </div>
      {window_script}
    """
    return respond(content, "Appeal your ban", 200)


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

# ... (rest of the file is unchanged until roblox_callback)

@router.get("/oauth/roblox/callback")
async def roblox_callback(request: Request, code: str, state: str, lang: Optional[str] = None):
    try:
        state_data = serializer.loads(state)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid state")

    current_lang = normalize_language(lang or state_data.get("lang"))
    strings = await get_strings(current_lang)

    token = await roblox_api.exchange_code_for_token(code)
    user = await roblox_api.get_user_info(token["access_token"])
    user_id = user["sub"]
    uname_label = user.get("name") or user.get("preferred_username")
    display_name = clean_display_name(user.get("nickname") or uname_label)

    ip = get_client_ip(request)
    state_id = state_data.get("state_id")
    if not validate_state_token(state_id, ip):
        raise HTTPException(status_code=400, detail="Invalid or replayed state")
    asyncio.create_task(send_log_message(f"[auth_roblox] user={user_id} ip_hash={hash_ip(ip)} lang={current_lang}"))

    history_html = "<div class='muted'>Appeal history for Roblox is not yet implemented.</div>"

    strings = dict(strings)
    strings["user_chip"] = build_user_chip({"type": "roblox", "display_name": display_name, "runame": uname_label})

    def respond(body_html: str, title: str, status_code: int = 200) -> HTMLResponse:
        resp = HTMLResponse(render_page(title, body_html, lang=current_lang, strings=strings), status_code=status_code, headers={"Cache-Control": "no-store"})
        persist_roblox_user_session(resp, user_id, uname_label, display_name=display_name)
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp

    ban = await roblox_api.get_live_ban_status(user_id)
    if not ban:
        content = f"""
          <div class="card status">
            <p>No active ban found for Roblox user {html.escape(uname_label)}.</p>
            <a class="btn" href="/">Back home</a>
          </div>
        """
        return respond(content, "No active ban", 200)

    ban_history = await roblox_api.get_ban_history(user_id)
    short_reason = shorten_public_ban_reason(ban.get("displayReason") or "")

    session_token = serializer.dumps(
        {
            "ruid": user_id,
            "runame": uname_label,
            "ban_data": ban,
            "ban_reason_short": short_reason,
            "ban_history": ban_history,
            "iat": time.time(),
            "lang": current_lang,
        }
    )
    ban_reason = html.escape(short_reason)
    user_id_label = html.escape(str(user_id))

    content = f"""
      <div class="grid-2">
        <div class="form-card">
          <h2 style="margin:8px 0;">Appeal your Roblox Ban</h2>
          <p class="muted">One appeal per ban. Be clear and concise.</p>
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
    return respond(content, "Appeal your Roblox Ban", 200)

@router.post("/roblox/submit")
async def roblox_submit(
    request: Request,
    session: str = Form(...),
    appeal_reason: str = Form(...),
):
    try:
        data = serializer.loads(session)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid session")

    now = time.time()
    if len(appeal_reason or "") > 2000:
        raise HTTPException(status_code=400, detail="Appeal reason too long. Please keep it under 2000 characters.")

    token_hash = hash_value(session)
    issued_at = float(data.get("iat", 0))
    if not issued_at or now - issued_at > SESSION_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="This form session expired. Please restart the appeal.")
    if _used_sessions.get(token_hash):
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")

    ip = get_client_ip(request)
    user_id = data["ruid"]
    enforce_ip_rate_limit(ip)
    asyncio.create_task(send_log_message(f"[roblox_appeal_attempt] user={user_id} ip_hash={hash_ip(ip)}"))

    last = _appeal_rate_limit.get(user_id)
    if last and now - last < APPEAL_COOLDOWN_SECONDS:
        wait = int(APPEAL_COOLDOWN_SECONDS - (now - last))
        raise HTTPException(status_code=429, detail=f"Please wait {wait} seconds before submitting another appeal.")
    _appeal_rate_limit[user_id] = now

    appeal_id = str(uuid.uuid4())[:8]

    if is_supabase_ready():
        await supabase_request(
            "post",
            ROBLOX_SUPABASE_TABLE,
            payload={
                "appeal_id": appeal_id,
                "roblox_id": user_id,
                "roblox_username": data["runame"],
                "appeal_text": appeal_reason,
                "ban_data": data.get("ban_data"),
                "short_ban_reason": data.get("ban_reason_short"),
                "ip_hash": hash_ip(ip),
            },
        )
    
    _used_sessions[token_hash] = now
    _appeal_locked[user_id] = True
    
    current_lang = data.get("lang", "en")
    strings = await get_strings(current_lang)
    success = f"""
      <div class="card">
        <h1>Appeal Submitted</h1>
        <p>Reference ID: <strong>{html.escape(appeal_id)}</strong></p>
        <p class="muted">Your Roblox appeal has been submitted for review.</p>
        <a class="btn" href="/">Back home</a>
      </div>
    """
    return HTMLResponse(render_page("Appeal Submitted", success, lang=current_lang, strings=strings), status_code=200, headers={"Cache-Control": "no-store"})

# ... (rest of the file is unchanged)


@router.post("/submit")
async def submit(
    request: Request,
    session: str = Form(...),
    evidence: str = Form("No evidence provided."),
    appeal_reason: str = Form(...),
):
    try:
        data = serializer.loads(session)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid session")

    now = time.time()
    if len(appeal_reason or "") > 2000:
        raise HTTPException(status_code=400, detail="Appeal reason too long. Please keep it under 2000 characters.")
    if len(evidence or "") > 1500:
        raise HTTPException(status_code=400, detail="Evidence too long. Please keep it concise.")

    token_hash = hash_value(session)
    issued_at = float(data.get("iat", 0))
    if not issued_at or now - issued_at > SESSION_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="This form session expired. Please restart the appeal.")
    if _used_sessions.get(token_hash):
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")
    if await is_session_token_used(token_hash):
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")

    first_seen = float(data.get("ban_first_seen", now))
    if now - first_seen > APPEAL_WINDOW_SECONDS:
        raise HTTPException(status_code=403, detail="This ban is older than the appeal window.")

    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    user_agent = request.headers.get("User-Agent", "unknown")
    enforce_ip_rate_limit(ip)
    asyncio.create_task(send_log_message(f"[appeal_attempt] user={data.get('uid')} ip_hash={hash_ip(ip)}"))

    remote_last = await get_remote_last_submit(data["uid"])
    last = _appeal_rate_limit.get(data["uid"])
    if remote_last:
        last = max(last or 0, remote_last)
    if last and now - last < APPEAL_COOLDOWN_SECONDS:
        wait = int(APPEAL_COOLDOWN_SECONDS - (now - last))
        raise HTTPException(status_code=429, detail=f"Please wait {wait} seconds before submitting another appeal.")
    _appeal_rate_limit[data["uid"]] = now

    appeal_id = str(uuid.uuid4())[:8]
    user = {"id": data["uid"], "username": data["uname"], "discriminator": "0"}
    user_lang = data.get("lang", "en")
    appeal_reason_en = await translate_text(appeal_reason, target_lang="en", source_lang=user_lang)
    reason_for_embed = appeal_reason_en
    if normalize_language(user_lang) != "en":
        reason_for_embed += f"\n(Original {user_lang}: {appeal_reason})"
    await post_appeal_embed(
        appeal_id=appeal_id,
        user=user,
        ban_reason=data.get("ban_reason") or "No reason provided.",
        ban_evidence=evidence or "No evidence provided.",
        appeal_reason=reason_for_embed,
    )

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

    msg_cache = data.get("message_cache") or []
    asyncio.create_task(
        send_log_message(
            f"[appeal_submitted] appeal={appeal_id} user={user['id']} ip_hash={hash_ip(ip)} lang={user_lang} ban_reason=\"{data.get('ban_reason','N/A')}\" msg_ctx={len(msg_cache)}"
        )
    )

    _used_sessions[token_hash] = now
    await mark_session_token(token_hash, user["id"], now)
    _appeal_locked[data["uid"]] = True
    stale_sessions = [token for token, ts in _used_sessions.items() if now - ts > SESSION_TTL_SECONDS * 2]
    for token in stale_sessions:
        _used_sessions.pop(token, None)

    strings = await get_strings(user_lang)
    success = f"""
      <div class="card">
        <h1>Appeal submitted</h1>
        <p>Reference ID: <strong>{html.escape(appeal_id)}</strong></p>
        <p class="muted">We will review your appeal shortly. You will be notified in Discord.</p>
        <a class="btn" href="/">Back home</a>
      </div>
    """
    return HTMLResponse(render_page("Appeal submitted", success, lang=user_lang, strings=strings), status_code=200, headers={"Cache-Control": "no-store"})
