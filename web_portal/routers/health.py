from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..bot import bot_client
from ..i18n import detect_language, get_strings
from ..services.supabase import is_supabase_ready
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
        "supabase_ready": is_supabase_ready(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # If caller prefers JSON (monitoring/uptime checks), return early
    if not wants_html(request):
        return data

    lang = await detect_language(request)
    strings = await get_strings(lang)

    bot_status = "Healthy" if online else "Offline"
    supabase_status = "Ready" if data["supabase_ready"] else "Unavailable"

    def chip(label: str, state: str) -> str:
        cls = "accepted" if state == "up" else "pending" if state == "degraded" else "declined"
        return f'<span class="status-chip {cls}">{label}</span>'

    status_cards = [
        {
            "title": "Discord integration",
            "state": "up" if online else "down",
            "label": bot_status,
            "body": "Receives moderation events and updates appeal activity.",
            "id": "discord",
        },
        {
            "title": "Database connectivity",
            "state": "up" if data["supabase_ready"] else "down",
            "label": supabase_status,
            "body": "Stores appeals and submission state.",
            "id": "database",
        },
        {
            "title": "Bot background task",
            "state": "up" if bot_task_state == "running" else "degraded" if bot_task_state in ("not_started", "done") else "down",
            "label": bot_task_state or "Unknown",
            "body": "Background worker that processes events.",
            "id": "worker",
        },
    ]

    cards_html = ""
    for item in status_cards:
        cards_html += f"""
        <div class="card" style="background:var(--card-bg-3);" id="card-{item['id']}">
          <div class="status-heading" style="display:flex;align-items:center;justify-content:space-between;">
            <h3>{item['title']}</h3>
            <span class="chip-wrapper" id="chip-{item['id']}">{chip(item['label'], item['state'])}</span>
          </div>
          <p class="muted small" id="desc-{item['id']}">{item['body']}</p>
        </div>
        """

    body = f"""
    <div class="card status-card">
      <h1>System status</h1>
      <p class="muted">Live public overview of appeal services.</p>

      <div class="grid" style="padding:0;">
        {cards_html}
      </div>

      <p class="small muted" style="margin-top:12px;" id="updated-at">Updated: {data["updated_at"]}</p>

      <script>
        const stateToLabel = (up) => up ? 'Healthy' : 'Offline';
        const stateToClass = (state) => state === 'up' ? 'accepted' : (state === 'degraded' ? 'pending' : 'declined');

        async function refreshHealth() {{
          try {{
            const resp = await fetch('/health', {{ headers: {{ 'Accept': 'application/json' }} }});
            if (!resp.ok) return;
            const data = await resp.json();

            const updates = [
              {{ id: 'discord', state: data.bot_online ? 'up' : 'down', label: stateToLabel(data.bot_online) }},
              {{ id: 'database', state: data.supabase_ready ? 'up' : 'down', label: stateToLabel(data.supabase_ready) }},
              {{ id: 'worker', state: data.bot_task === 'running' ? 'up' : (data.bot_task === 'not_started' || data.bot_task === 'done' ? 'degraded' : 'down'), label: data.bot_task || 'Unknown' }},
            ];

            updates.forEach(item => {{
              const chipWrap = document.getElementById(`chip-${{item.id}}`);
              if (chipWrap) {{
                chipWrap.innerHTML = `<span class="status-chip ${{stateToClass(item.state)}}">${{item.label}}</span>`;
              }}
            }});

            const ts = data.updated_at || new Date().toISOString();
            const upd = document.getElementById('updated-at');
            if (upd) upd.textContent = `Updated: ${{ts}}`;
          }} catch (e) {{
            console.warn('Health refresh failed', e);
          }}
        }}

        setInterval(refreshHealth, 60000);
      </script>

      <div class="btn-row" style="margin-top:16px;">
        <a class="btn secondary" href="/">Home</a>
        <a class="btn btn--ghost" href="/status">Appeals</a>
      </div>
    </div>
    """

    return HTMLResponse(
        render_page("Health", body, lang=lang, strings=strings),
        headers={"Cache-Control": "no-store"},
    )
