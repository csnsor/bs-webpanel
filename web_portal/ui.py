from __future__ import annotations

import html
import secrets
import time
from typing import Dict, List, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse

from .clients import JINJA_ENV
from .i18n import LANG_STRINGS
from .settings import INVITE_LINK
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
    top_actions = strings.get("top_actions") or strings.get("user_chip", "")
    script_block = strings.get("script_block")
    script_nonce = strings.get("script_nonce") or secrets.token_urlsafe(12)
    full_script = script_block or ""
    csp = (
        "default-src 'self'; "
        "img-src 'self' data: https://*.discordapp.com https://*.discord.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://discord.com https://*.discord.com; "
    )
    favicon = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%237c5cff'/%3E%3Cpath d='M42 10 28 24l4 4-6 6 4 4-6 6-6-6 6-6-4-4 6-6 4 4 6-6 4 4 6-6-10-10Z' fill='white'/%3E%3C/svg%3E"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="color-scheme" content="dark" />
        <title>{html.escape(title)}</title>
        <link rel="icon" type="image/svg+xml" href="{favicon}">
        <meta http-equiv="Content-Security-Policy" content="{csp}">
        <link rel="stylesheet" href="/static/styles.css">
      </head>
      <body>
        <div class="bg-orbit" aria-hidden="true"></div>
        <div class="bg-grid" aria-hidden="true"></div>

        <header class="top">
          <div class="wrap top__inner">
            <a class="brand" href="/">
              <span class="brand__mark" aria-hidden="true">
                <span class="mark__ring"></span>
                <span class="mark__core">BS</span>
              </span>
              <span class="brand__text">
                <span class="brand__name">BlockSpin</span>
                <span class="brand__tag">Ban Appeal Portal</span>
              </span>
            </a>

            <nav class="nav">
              <a class="nav__link" href="/status">Appeal Status</a>
              <a class="nav__link" href="/tos">Terms</a>
              <a class="nav__link" href="/privacy">Privacy</a>
              <a class="nav__link nav__link--muted" href="{INVITE_LINK}" rel="noreferrer">Discord</a>
            </nav>

            {top_actions}
          </div>
        </header>

        <main class="wrap">
          {body_html}

          <footer class="footer">
            <div class="footer__left">
              <span class="footer__brand">BlockSpin</span>
              <span class="footer__muted">© {year}</span>
            </div>
            <div class="footer__right">
              <a href="/tos">Terms</a>
              <a href="/privacy">Privacy</a>
              <a href="/status">Status</a>
              <a href="?lang={toggle_lang}" style="color:inherit;">{toggle_label}</a>
            </div>
          </footer>
        </main>

        <script nonce="{script_nonce}">{full_script}</script>
      </body>
    </html>
    """


def build_user_chip(
    session: Optional[dict],
    *,
    discord_login_url: Optional[str] = None,
    roblox_login_url: Optional[str] = None,
) -> str:
    if not session:
        # Not logged in, show both login buttons
        return f"""
          <div class="top__actions">
            <a class="btn btn--discord" href="{html.escape(discord_login_url or '#')}" aria-label="Login with Discord">
              Login with Discord
            </a>
            <a class="btn btn--roblox" href="{html.escape(roblox_login_url or '#')}" aria-label="Login with Roblox">
              Login with Roblox
            </a>
          </div>
        """

    # User is logged in
    name = clean_display_name(session.get("display_name") or "")
    has_discord = "uid" in session
    has_roblox = "ruid" in session

    buttons = []
    
    if name:
        buttons.append(f"<span class='greeting'>Hi, {html.escape(name)}</span>")

    if has_discord and not has_roblox and roblox_login_url:
        buttons.append(
            f"<a class='btn btn--roblox' href='{html.escape(roblox_login_url)}' target='_blank' rel='noopener noreferrer'>Link Roblox</a>"
        )
    
    if has_roblox and not has_discord and discord_login_url:
        buttons.append(
            f"<a class='btn btn--discord' href='{html.escape(discord_login_url)}' target='_blank' rel='noopener noreferrer'>Link Discord</a>"
        )

    if has_discord and has_roblox:
        buttons.append("<span class='chip chip--ok'>Accounts Linked</span>")

    buttons.append("<a class='btn btn--primary' href='/status'>Appeal Status</a>")
    buttons.append("<a class='btn btn--ghost' href='/logout'>Logout</a>")

    return f'<div class="top__actions">{" ".join(buttons)}</div>'



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
