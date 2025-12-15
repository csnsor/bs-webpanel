from __future__ import annotations

import html
import secrets
import time
from typing import Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from .clients import JINJA_ENV
from .i18n import LANG_STRINGS
from .utils import clean_display_name, normalize_language

HISTORY_TEMPLATE = JINJA_ENV.from_string(
    """
<ul class="history-list">
{% for item in history %}
  {% set status = (item.get("status") or "pending").lower() %}
  {% set status_class = "pending" %}
  {% if status.startswith("accept") %}{% set status_class = "accepted" %}
  {% elif status.startswith("decline") %}{% set status_class = "declined" %}
  {% endif %}
  <li class="history-item">
    <div class="status-chip {{ status_class }}">{{ status.title() }}</div>
    <div class="meta"><strong>Reference:</strong> {{ item.get("appeal_id") or "-" }}</div>
    <div class="meta"><strong>Submitted:</strong> {{ format_timestamp(item.get("created_at") or "") }}</div>
    <div class="meta"><strong>Ban reason:</strong> {{ item.get("ban_reason") or "No ban reason recorded." }}</div>
    <div class="meta"><strong>Appeal:</strong> {{ item.get("appeal_reason") or "No appeal reason captured." }}</div>
  </li>
{% endfor %}
</ul>
"""
)


def render_history_items(history: List[dict], *, format_timestamp) -> str:
    if not history:
        return "<div class='muted'>No appeals yet.</div>"
    return HISTORY_TEMPLATE.render(history=history, format_timestamp=format_timestamp)


def render_page(title: str, body_html: str, lang: str = "en", strings: Optional[Dict[str, str]] = None) -> str:
    lang = normalize_language(lang)
    year = time.gmtime().tm_year
    strings = strings or LANG_STRINGS["en"]
    toggle_lang = "es" if lang != "es" else "en"
    toggle_label = strings.get("language_switch", "Switch language")
    user_chip = strings.get("user_chip", "")
    script_block = strings.get("script_block")
    script_nonce = strings.get("script_nonce") or secrets.token_urlsafe(12)
    base_script = """
    (function() {
      const banner = document.getElementById('liveBanner');
      if (!banner) return;
      const dot = document.getElementById('liveBannerDot');
      const valueEl = document.getElementById('liveBannerValue');
      const historyEl = document.getElementById('live-history');

      function escapeHtml(input) {
        return String(input || '')
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/\"/g, '&quot;')
          .replace(/'/g, '&#39;');
      }

      function simplifyReason(reason) {
        const text = String(reason || '').trim();
        if (!text) return '';
        const idx = text.lastIndexOf(':');
        if (idx === -1) return text;
        const tail = text.slice(idx + 1).trim();
        return tail || text;
      }

      function renderHistory(history) {
        if (!historyEl) return;
        if (!history || !history.length) return;
        const rows = history.map(item => {
          const status = String(item.status || 'pending').toLowerCase();
          const statusClass = status.startsWith('accept') ? 'accepted' : status.startsWith('decline') ? 'declined' : 'pending';
          const created = escapeHtml(item.created_at || '');
          const safeRef = escapeHtml(item.appeal_id || '');
          const safeBan = escapeHtml(simplifyReason(item.ban_reason || ''));
          const label = status.startsWith('accept') ? 'Accepted' : status.startsWith('decline') ? 'Declined' : 'Pending';
          return `
            <li class="history-item">
              <div class="status-chip ${statusClass}">${label}</div>
              <div class="meta">Reference: ${safeRef || '-'}</div>
              <div class="meta">Submitted: ${created}</div>
              <div class="meta">Ban reason: ${safeBan || '-'}</div>
            </li>
          `;
        }).join('');
        historyEl.innerHTML = `<ul class='history-list'>${rows}</ul>`;
      }
      async function tick() {
        try {
          const res = await fetch('/status/data', { headers: { 'Accept': 'application/json' }});
          if (!res.ok) throw new Error('status ' + res.status);
          const data = await res.json();
          const history = data.history || [];
          if (!history.length) {
            valueEl.textContent = 'No recent updates.';
            dot.classList.remove('warn');
            return;
          }
          const latest = history[0];
          const status = (latest.status || 'pending').toLowerCase();
          const ref = latest.appeal_id || 'n/a';
          const label = status.startsWith('accept') ? 'Accepted' : status.startsWith('decline') ? 'Declined' : 'Pending';
          valueEl.textContent = label + ' · ref ' + ref;
          dot.classList.remove('warn');
          renderHistory(history);
        } catch (e) {
          valueEl.textContent = 'Live updates unavailable.';
          dot.classList.add('warn');
        }
      }
      tick();
      setInterval(tick, 15000);
    })();
    """
    full_script = base_script + "\n" + (script_block or "")
    csp = (
        "default-src 'self'; "
        "img-src 'self' data: https://*.discordapp.com https://*.discord.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://discord.com https://*.discord.com; "
    )
    favicon = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%235865F2'/%3E%3Cpath d='M42 10 28 24l4 4-6 6 4 4-6 6-6-6 6-6-4-4 6-6 4 4 6-6 4 4 6-6-10-10Z' fill='white'/%3E%3C/svg%3E"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)}</title>
        <link rel="icon" type="image/svg+xml" href="{favicon}">
        <meta http-equiv="Content-Security-Policy" content="{csp}">
        <link rel="stylesheet" href="/static/styles.css">
      </head>
      <body>
        <div class="app">

          <div class="brand-row">
            <div class="brand">
              <div class="logo">BS</div>
              <div class="brand-text">
                <h1>BlockSpin</h1>
                <span>Appeals Portal</span>
              </div>
            </div>
            <div class="live-banner" id="liveBanner" aria-live="polite">
              <span class="dot" id="liveBannerDot" aria-hidden="true"></span>
              <div class="meta">
                <div class="title">Live updates</div>
                <div class="value" id="liveBannerValue">Loading…</div>
              </div>
            </div>
            {user_chip}
          </div>

          {body_html}

          <div class="footer">
            <div>&copy; {year} BlockSpin Community</div>
            <div style="margin-top:8px;">
              <a href="?lang={toggle_lang}" style="color:inherit; text-decoration:none; border-bottom:1px dotted #555;">{toggle_label}</a>
            </div>
          </div>
        </div>

        <script nonce="{script_nonce}">
            {full_script}
        </script>
      </body>
    </html>
    """


def build_user_chip(session: Optional[dict]) -> str:
    if not session:
        return ""
    name = clean_display_name(session.get("display_name") or session.get("uname") or "")
    return f"""
      <div class="user-chip">
        <span class="name">{html.escape(name)}</span>
        <div class="actions"><a href="/logout">Logout</a></div>
      </div>
    """


def render_error(
    title: str,
    message: str,
    *,
    status_code: int = 400,
    lang: str = "en",
    strings: Optional[Dict[str, str]] = None,
) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_msg = html.escape(message)
    strings = strings or LANG_STRINGS["en"]
    content = f"""
      <div class="card" style="text-align:center;">
        <div class="icon-error">!</div>
        <h2>{safe_title}</h2>
        <p>{safe_msg}</p>
        <div class="error-box">{safe_msg}</div>
        <div class="btn-row" style="justify-content:center;">
          <a class="btn" href="/" aria-label="Back home">{strings['error_home']}</a>
          <a class="btn secondary" href="javascript:location.reload();" aria-label="Retry action">{strings['error_retry']}</a>
        </div>
      </div>
    """
    return HTMLResponse(
        render_page(title, content, lang=lang, strings=strings),
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )

