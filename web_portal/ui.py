from __future__ import annotations

import html
import secrets
import time
import json
from typing import Dict, List, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse

from .clients import JINJA_ENV
from .i18n import LANG_STRINGS, LANG_META
from .settings import INVITE_LINK
from .utils import clean_display_name, normalize_language
from . import state

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
    <div class="history-item__header">
      <div class="status-chip {{ status_class }}">{{ status.title() }}</div>
      {% if item.get("platform") %}
        <span class="chip chip--ghost">{{ item.get("platform") }}</span>
      {% endif %}
    </div>
    <div class="meta"><strong>Reference:</strong> {{ item.get("appeal_id") or "-" }}</div>
    <div class="meta"><strong>Submitted:</strong> {{ format_timestamp(item.get("created_at") or "") }}</div>
    <div class="meta"><strong>Moderator:</strong> {{ item.get("moderator") or "Pending review" }}</div>
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
    top_actions = strings.get("top_actions") or strings.get("user_chip", "")
    script_block = strings.get("script_block")
    script_nonce = strings.get("script_nonce") or secrets.token_urlsafe(12)
    full_script = script_block or ""
    lang_switch_label = html.escape(strings.get("language_switch", "Switch language"))
    current_flag = html.escape((LANG_META.get(lang) or {}).get("flag", "🌐"))
    lang_options: List[str] = []
    for code, meta in LANG_META.items():
        label = html.escape(meta.get("name") or code.upper())
        flag = html.escape(meta.get("flag") or "")
        active_cls = "lang-option--active" if code == lang else ""
        lang_options.append(
            f'<button class="lang-option {active_cls}" data-lang="{code}" aria-label="{label}"><span class="lang-flag">{flag}</span><span class="lang-name">{label}</span></button>'
        )
    lang_popover = (
        f'<div class="lang-popover" id="langPopover" role="menu">{"".join(lang_options)}</div>'
    )
    nav_how_it_works = html.escape(strings.get("nav_how_it_works", strings.get("how_it_works", "How it works")))
    nav_terms = html.escape(strings.get("nav_terms", "Terms"))
    nav_privacy = html.escape(strings.get("nav_privacy", "Privacy"))
    nav_status = html.escape(strings.get("nav_status", "Appeal Status"))
    nav_discord = html.escape(strings.get("nav_discord", "Discord"))
    brand_tag = html.escape(strings.get("brand_tag", "Ban Appeal Portal"))
    csp = (
        "default-src 'self'; "
        "img-src 'self' data: https://*.discordapp.com https://*.discord.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://discord.com https://*.discord.com; "
    )
    favicon = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%237c5cff'/%3E%3Cpath d='M42 10 28 24l4 4-6 6 4 4-6 6-6-6 6-6-4-4 6-6 4 4 6-6 4 4 6-6-10-10Z' fill='white'/%3E%3C/svg%3E"
    announcement_html = ""
    current_announcement = getattr(state, "_announcement_text", None)
    current_epoch = getattr(state, "_session_epoch", 0)
    announce_block = f"window.BS_ANNOUNCE = {json.dumps({'text': current_announcement, 'epoch': current_epoch})};"
    live_script = """
      (function(){
        const banner = document.getElementById("live-announcement");
        let local = (window.BS_ANNOUNCE || {epoch:0,text:null});
        function render(text) {
          if (!banner) return;
          banner.innerHTML = "";
          if (!text) { return; }
          const card = document.createElement("div");
          card.className = "announcement-card";
          card.innerHTML = `
            <div class="announcement__left">
              <div class="announcement__dot"></div>
              <div class="announcement__copy">
                <div class="announcement__label">Announcement</div>
                <div class="announcement__text"></div>
              </div>
            </div>
            <div class="announcement__badge">Live</div>
          `;
          card.querySelector(".announcement__text").textContent = text;
          banner.appendChild(card);
        }
        render(local.text);
        async function tick(){
          try{
            const resp = await fetch('/live/announcement',{headers:{'Accept':'application/json'}});
            if(!resp.ok) return;
            const data = await resp.json();
            if(typeof data.epoch === 'number' && data.epoch > (local.epoch||0)){
              window.location.reload();
              return;
            }
            local = {epoch:data.epoch||local.epoch,text:data.announcement||null};
            render(local.text);
          }catch(e){}
        }
        setInterval(tick, 10000);
      })();
    """
    lang_script = """
        (function(){
          const toggles = Array.from(document.querySelectorAll('.lang-toggle'));
          const pop = document.getElementById('langPopover');
          if(!toggles.length || !pop) return;
          const setExpanded = (state) => toggles.forEach(btn => btn.setAttribute('aria-expanded', state ? 'true' : 'false'));
          const open = () => { pop.classList.add('open'); setExpanded(true); };
          const close = () => { pop.classList.remove('open'); setExpanded(false); };
          const togglePop = (e) => { e.preventDefault(); pop.classList.contains('open') ? close() : open(); };
          toggles.forEach(btn => btn.addEventListener('click', togglePop));
          document.addEventListener('click', (e) => {
            if (pop.contains(e.target) || toggles.some(btn => btn.contains(e.target))) return;
            close();
          });
          pop.querySelectorAll('.lang-option').forEach(btn => {
            btn.addEventListener('click', () => {
              const code = btn.dataset.lang;
              if (!code) return;
              const url = new URL(window.location.href);
              url.searchParams.set('lang', code);
              document.cookie = `lang=${code}; path=/; max-age=${60*60*24*30}; samesite=Lax`;
              window.location.href = url.toString();
            });
          });
        })();
    """

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="color-scheme" content="dark" />
        <title>{html.escape(title)}</title>
        <meta property="og:type" content="website" />
        <meta property="og:title" content="BlockSpin Appeals" />
        <meta property="og:description" content="Link Discord + Roblox, see unified appeal history, and submit your ban appeal to BlockSpin moderators." />
        <meta property="og:url" content="https://bs-appeals.up.railway.app" />
        <meta property="og:image" content="https://bs-appeals.up.railway.app/static/og-banner.png" />
        <meta name="twitter:card" content="summary_large_image" />
        <meta name="twitter:title" content="BlockSpin Appeals" />
        <meta name="twitter:description" content="Link Discord + Roblox, see unified appeal history, and submit your ban appeal to BlockSpin moderators." />
        <meta name="twitter:image" content="https://bs-appeals.up.railway.app/static/og-banner.png" />
        <link rel="icon" type="image/svg+xml" href="{favicon}">
        <meta http-equiv="Content-Security-Policy" content="{csp}">
        <link rel="stylesheet" href="/static/styles.css">
        <style>
          .lang-switch {{ position: relative; }}
          .lang-toggle {{ display:flex;align-items:center;gap:6px;border:1px solid var(--border);background:var(--card-bg-2);color:inherit;padding:8px 10px;border-radius:10px;cursor:pointer; }}
          .lang-toggle .lang-flag {{ font-size:16px; }}
          .lang-popover {{ position:absolute;top:110%;right:0;background:var(--card-bg-2);border:1px solid var(--border);border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,0.25);padding:8px;display:none;z-index:30;min-width:180px; }}
          .lang-popover.open {{ display:block; }}
          .lang-option {{ width:100%;display:flex;align-items:center;gap:8px;padding:8px 10px;border:none;background:transparent;color:inherit;border-radius:8px;cursor:pointer;text-align:left; }}
          .lang-option:hover {{ background:var(--card-bg-3); }}
          .lang-option--active {{ outline:1px solid var(--border-strong, #5c5cff); background:var(--card-bg-3); }}
          .lang-flag {{ width:20px; text-align:center; font-family: "Twemoji", "Noto Color Emoji", "Segoe UI Emoji", system-ui; }}
          .lang-name {{ flex:1; font-weight:600; }}
          @media (max-width: 768px) {{
            .lang-popover {{ left:0; right:auto; }}
          }}
        </style>
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
                <span class="brand__tag">{brand_tag}</span>
              </span>
            </a>

            <nav class="nav">
              <a class="nav__link" href="/how-it-works">{nav_how_it_works}</a>
              <a class="nav__link" href="/tos">{nav_terms}</a>
              <a class="nav__link" href="/privacy">{nav_privacy}</a>
              <a class="nav__link" href="/status">{nav_status}</a>
              <a class="nav__link nav__link--muted" href="{INVITE_LINK}" rel="noreferrer">{nav_discord}</a>
            </nav>

            {top_actions}
          </div>
        </header>

        <main class="wrap">
          <div id="live-announcement">{announcement_html}</div>
          {body_html}

          <footer class="footer">
            <div class="footer__left">
              <span class="footer__brand">BlockSpin</span>
              <span class="footer__muted">© {year}</span>
            </div>
            <div class="footer__right">
              <a href="/tos">{nav_terms}</a>
              <a href="/privacy">{nav_privacy}</a>
              <a href="/status">{nav_status}</a>
              <div class="lang-switch">
                <button class="lang-toggle" id="footerLangToggle" aria-haspopup="true" aria-expanded="false">
                  <span class="lang-flag">{current_flag}</span>
                  <span class="lang-label">{lang_switch_label}</span>
                </button>
                {lang_popover}
              </div>
            </div>
          </footer>
        </main>

        <script nonce="{script_nonce}">{announce_block}{full_script}{lang_script}{live_script}</script>
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
