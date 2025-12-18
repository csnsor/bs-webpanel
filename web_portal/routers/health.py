from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..bot import bot_client
from ..i18n import detect_language, get_strings
from ..services.supabase import is_supabase_ready
from ..settings import MESSAGE_CACHE_GUILD_ID, SUPABASE_CONTEXT_TABLE, TARGET_GUILD_ID
from ..state import _bot_task
from ..ui import render_page
from ..utils import wants_html

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    online = False
    try:
        online = bool(bot_client and getattr(bot_client, "is_ready", lambda: False)())
    except Exception:
        online = False
    bot_task_state = None
    try:
        if _bot_task is None:
            bot_task_state = "not_started"
        elif _bot_task.cancelled():
            bot_task_state = "cancelled"
        elif _bot_task.done():
            bot_task_state = "done"
        else:
            bot_task_state = "running"
    except Exception:
        bot_task_state = "unknown"
    data = {
        "ok": True,
        "bot_online": online,
        "bot_task": bot_task_state,
        "target_guild_id": TARGET_GUILD_ID,
        "message_cache_guild_id": MESSAGE_CACHE_GUILD_ID,
        "supabase_ready": is_supabase_ready(),
        "supabase_context_table": SUPABASE_CONTEXT_TABLE,
    }

    # If caller prefers JSON (monitoring/uptime checks), return early
    if not wants_html(request):
        return data

    lang = await detect_language(request)
    strings = await get_strings(lang)

    bot_status = "Healthy" if online else "Offline"
    supabase_status = "Ready" if data["supabase_ready"] else "Unavailable"
    chip = lambda label, ok: f'<span class="status-chip {"accepted" if ok else "declined"}">{label}</span>'

    body = f"""
      <div class="card status-card">
        <div class="status-heading">
          <h1>Portal health</h1>
          <p class="muted">Live status for key services powering appeals.</p>
        </div>
        <div class="grid" style="padding:0;">
          <div class="card" style="background:var(--card-bg-3);">
            <div class="status-heading">
              <h3>Discord Bot</h3>
              {chip(bot_status, online)}
            </div>
            <p class="muted small">Tracks bans and posts embeds.</p>
            <div class="kv">
              <div class="kv-row"><div class="k">Task</div><div class="v">{bot_task_state}</div></div>
              <div class="kv-row"><div class="k">Target Guild</div><div class="v">{TARGET_GUILD_ID}</div></div>
              <div class="kv-row"><div class="k">Message Cache Guild</div><div class="v">{MESSAGE_CACHE_GUILD_ID}</div></div>
            </div>
          </div>
          <div class="card" style="background:var(--card-bg-3);">
            <div class="status-heading">
              <h3>Supabase</h3>
              {chip(supabase_status, data["supabase_ready"])}
            </div>
            <p class="muted small">Persists appeals, sessions, and context.</p>
            <div class="kv">
              <div class="kv-row"><div class="k">Context table</div><div class="v">{SUPABASE_CONTEXT_TABLE}</div></div>
              <div class="kv-row"><div class="k">Status</div><div class="v">{supabase_status}</div></div>
            </div>
          </div>
        </div>
        <div class="btn-row" style="margin-top:12px;">
          <a class="btn secondary" href="/">Back home</a>
          <a class="btn btn--ghost" href="/status">Appeal status</a>
        </div>
      </div>
    """

    return HTMLResponse(
        render_page("Health", body, lang=lang, strings=strings),
        headers={"Cache-Control": "no-store"},
    )
