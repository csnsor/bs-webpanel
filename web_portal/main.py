import os
import secrets
import uuid
import asyncio
import time
import html
import copy
from typing import Optional, Tuple, Dict, List

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv
import logging

# Load .env if present (Railway still uses real env vars)
load_dotenv()


# --- Configuration ---
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")  # e.g. https://bs-appeals.up.railway.app/callback
DISCORD_BOT_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY")  # Required for interaction verification
TARGET_GUILD_ID = os.getenv("TARGET_GUILD_ID", "0")
MODERATOR_ROLE_ID = int(os.getenv("MODERATOR_ROLE_ID", "1353068159346671707"))
APPEAL_CHANNEL_ID = int(os.getenv("APPEAL_CHANNEL_ID", "1352973388334764112"))
APPEAL_LOG_CHANNEL_ID = int(os.getenv("APPEAL_LOG_CHANNEL_ID", "1353445286457901106"))
SECRET_KEY = os.getenv("PORTAL_SECRET_KEY") or secrets.token_hex(16)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "discord-appeals"
INVITE_LINK = "https://discord.gg/blockspin"

OAUTH_SCOPES = "identify guilds.join"
DISCORD_API_BASE = "https://discord.com/api/v10"

# Fail fast if required configuration is missing to avoid 502/503 crashes
_missing_envs = [
    name
    for name, val in {
        "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
        "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
        "DISCORD_REDIRECT_URI": DISCORD_REDIRECT_URI,
        "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
        "DISCORD_PUBLIC_KEY": DISCORD_PUBLIC_KEY,
    }.items()
    if not val
]
if _missing_envs:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing_envs)}")

# --- Basic app setup ---
app = FastAPI(title="BlockSpin Appeals Portal")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logging.basicConfig(level=logging.INFO)
serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response

# simple in-memory stores
_appeal_rate_limit: Dict[str, float] = {}  # {user_id: timestamp_of_last_submit}
_used_sessions: Dict[str, float] = {}  # {session_token: timestamp_used}
_ip_requests: Dict[str, List[float]] = {}  # {ip: [timestamps]}
_ban_first_seen: Dict[str, float] = {}  # {user_id: first time we saw the ban}
_appeal_locked: Dict[str, bool] = {}  # {user_id: True if appealed already}
_user_tokens: Dict[str, str] = {}  # {user_id: last OAuth access token}
_processed_appeals: Dict[str, float] = {}  # {appeal_id: timestamp_processed}
_declined_users: Dict[str, bool] = {}  # {user_id: True if appeal declined}
APPEAL_COOLDOWN_SECONDS = int(os.getenv("APPEAL_COOLDOWN_SECONDS", "300"))  # 5 minutes by default
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "900"))  # sessions expire after 15 minutes
APPEAL_IP_MAX_REQUESTS = int(os.getenv("APPEAL_IP_MAX_REQUESTS", "8"))
APPEAL_IP_WINDOW_SECONDS = int(os.getenv("APPEAL_IP_WINDOW_SECONDS", "60"))
APPEAL_WINDOW_SECONDS = int(os.getenv("APPEAL_WINDOW_SECONDS", str(7 * 24 * 3600)))  # 7 days default
DM_GUILD_ID = os.getenv("DM_GUILD_ID")  # optional: holding guild to enable DMs
REMOVE_FROM_DM_GUILD_AFTER_DM = os.getenv("REMOVE_FROM_DM_GUILD_AFTER_DM", "true").lower() == "true"
CLEANUP_DM_INVITES = os.getenv("CLEANUP_DM_INVITES", "true").lower() == "true"
PERSIST_SESSION_SECONDS = int(os.getenv("PERSIST_SESSION_SECONDS", str(7 * 24 * 3600)))  # keep users signed in
SESSION_COOKIE_NAME = "bs_session"

BASE_STYLES = """
:root {
  --bg: #06070c;
  --panel: #0b0f1b;
  --card: #101626;
  --border: #1d2538;
  --text: #f0f4ff;
  --muted: #9fb2d4;
  --accent: #6bc5ff;
  --accent-2: #7af7c8;
  --danger: #ff8a8a;
  --success: #7ef2c1;
  --shadow: 0 32px 120px rgba(0,0,0,0.45);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 18px 80px;
  min-height: 100vh;
  font-family: "DM Sans", "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
  background: radial-gradient(circle at 20% 20%, rgba(107,197,255,0.12), transparent 25%),
              radial-gradient(circle at 80% 0%, rgba(122,247,200,0.12), transparent 25%),
              linear-gradient(145deg, #05060c 0%, #090d17 45%, #05060c 100%);
  color: var(--text);
}
.shell {
  width: 100%;
  max-width: 1120px;
  margin: 0 auto;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 18px;
}
.hero {
  background: linear-gradient(135deg, rgba(107,197,255,0.12), rgba(122,247,200,0.04));
  border: 1px solid rgba(107,197,255,0.25);
  border-radius: 18px;
  padding: 28px;
  box-shadow: var(--shadow);
  display: grid;
  grid-template-columns: 1.2fr 0.9fr;
  gap: 18px;
  align-items: center;
}
.hero h1 {
  font-size: 2.2rem;
  margin: 0 0 12px;
  letter-spacing: -0.02em;
}
.hero .lead { color: var(--muted); line-height: 1.7; margin: 0 0 14px; }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-radius: 999px;
  background: rgba(107,197,255,0.12);
  border: 1px solid rgba(107,197,255,0.25);
  color: var(--accent);
  font-weight: 600;
  font-size: 0.9rem;
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  background: linear-gradient(120deg, var(--accent), #4f8ff6);
  color: #05060c;
  text-decoration: none;
  font-weight: 700;
  letter-spacing: -0.01em;
  padding: 13px 16px;
  border-radius: 12px;
  border: 1px solid rgba(107,197,255,0.4);
  transition: transform 120ms ease, box-shadow 120ms ease, background 120ms ease;
  box-shadow: 0 14px 30px rgba(79,143,246,0.25);
}
.btn:hover { transform: translateY(-1px); box-shadow: 0 16px 34px rgba(79,143,246,0.32); }
.btn.secondary {
  background: #0d1424;
  color: var(--text);
  border: 1px solid var(--border);
  box-shadow: none;
}
.btn-row { display: flex; flex-wrap: wrap; gap: 12px; }
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 18px;
  box-shadow: var(--shadow);
}
.card h2 { margin: 0 0 8px; font-size: 1.2rem; }
.muted { color: var(--muted); font-size: 0.95rem; }
.features { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 12px; }
.feature {
  background: #0d1322;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
}
.feature strong { display: block; margin-bottom: 6px; }
.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 10px;
  font-size: 0.85rem;
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border);
}
.status-chip.accepted { color: var(--success); border-color: rgba(126,242,193,0.4); }
.status-chip.declined { color: var(--danger); border-color: rgba(255,138,138,0.4); }
.status-chip.pending { color: var(--accent); border-color: rgba(107,197,255,0.4); }
.history-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 12px; }
.history-item {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  background: #0c111d;
}
.history-item .meta { color: var(--muted); font-size: 0.9rem; }
.field { text-align: left; margin-bottom: 14px; }
.field label { display: block; font-weight: 700; margin-bottom: 6px; }
input[type=text], textarea {
  width: 100%;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: #0d1322;
  color: var(--text);
  padding: 11px;
  font-size: 0.97rem;
}
textarea { resize: vertical; min-height: 140px; }
.form-card { padding: 18px; background: #0d1322; border: 1px solid var(--border); border-radius: 16px; }
.badge { display: inline-block; padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,0.05); border: 1px solid var(--border); font-size: 0.85rem; }
.status {
  margin-top: 12px;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: #0d1322;
}
.status.danger { border-color: rgba(255,138,138,0.35); color: var(--danger); }
.status.success { border-color: rgba(126,242,193,0.35); color: var(--success); }
.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}
.footer { margin-top: 16px; font-size: 0.9rem; color: var(--muted); text-align: left; }
.timeline { display: grid; gap: 10px; margin-top: 10px; }
.timeline .step { border-left: 3px solid var(--border); padding-left: 10px; color: var(--muted); }
.callout { border: 1px dashed rgba(107,197,255,0.4); border-radius: 12px; padding: 10px; color: var(--muted); background: rgba(107,197,255,0.06); }
@media (max-width: 780px) { .hero { grid-template-columns: 1fr; } }
"""


# --- Helpers ---
def is_supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def persist_user_session(response: Response, user_id: str, username: str):
    token = serializer.dumps({"uid": user_id, "uname": username, "iat": time.time()})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=PERSIST_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="Lax",
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


async def supabase_request(method: str, table: str, *, params: Optional[dict] = None, payload: Optional[dict] = None):
    if not is_supabase_ready():
        return None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(method, url, params=params, headers=headers, json=payload)
            resp.raise_for_status()
            if resp.content:
                return resp.json()
    except Exception as exc:
        logging.warning("Supabase request failed table=%s method=%s error=%s", table, method, exc)
    return None


async def log_appeal_to_supabase(
    appeal_id: str,
    user: dict,
    ban_reason: str,
    ban_evidence: str,
    appeal_reason: str,
    ip: str,
    forwarded_for: str,
    user_agent: str,
):
    payload = {
        "appeal_id": appeal_id,
        "user_id": user["id"],
        "username": user.get("username"),
        "guild_id": TARGET_GUILD_ID,
        "ban_reason": ban_reason,
        "ban_evidence": ban_evidence,
        "appeal_reason": appeal_reason,
        "status": "pending",
        "ip": ip,
        "forwarded_for": forwarded_for,
        "user_agent": user_agent,
    }
    await supabase_request("post", SUPABASE_TABLE, payload=payload)


async def update_appeal_status(
    appeal_id: str,
    status: str,
    moderator_id: Optional[str],
    dm_delivered: bool,
    notes: Optional[str] = None,
):
    payload = {
        "status": status,
        "decision_by": moderator_id,
        "decision_at": int(time.time()),
        "dm_delivered": dm_delivered,
        "notes": notes,
    }
    await supabase_request("patch", SUPABASE_TABLE, params={"appeal_id": f"eq.{appeal_id}"}, payload=payload)


async def fetch_appeal_history(user_id: str, limit: int = 6) -> List[dict]:
    records = await supabase_request(
        "get",
        SUPABASE_TABLE,
        params={"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": limit},
    )
    return records or []


def render_history_items(history: List[dict]) -> str:
    if not history:
        return "<div class='muted'>No recorded appeals yet. Once you submit, your history will appear here.</div>"
    items = []
    for item in history:
        status = (item.get("status") or "pending").lower()
        status_class = "pending"
        if status.startswith("accept"):
            status_class = "accepted"
        elif status.startswith("decline"):
            status_class = "declined"
        appeal_reason = html.escape(item.get("appeal_reason") or "No appeal reason captured.")
        ban_reason = html.escape(item.get("ban_reason") or "No ban reason recorded.")
        created_at = html.escape(str(item.get("created_at") or ""))
        items.append(
            f"""
            <li class="history-item">
              <div class="status-chip {status_class}">{status.title()}</div>
              <div class="meta">Reference: {html.escape(item.get("appeal_id") or '-')}</div>
              <div class="meta">Submitted: {created_at}</div>
              <div class="meta">Ban reason: {ban_reason}</div>
              <div class="meta">Appeal: {appeal_reason}</div>
            </li>
            """
        )
    return f"<ul class='history-list'>{''.join(items)}</ul>"


def render_page(title: str, body_html: str) -> str:
    return f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)}</title>
        <meta http-equiv="Content-Security-Policy" content="default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self' https://discord.com https://*.discord.com;">
        <style>{BASE_STYLES}</style>
      </head>
      <body>
        <div class="shell">
          {body_html}
        </div>
      </body>
    </html>
    """

def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept


def render_error(title: str, message: str, status_code: int = 400) -> HTMLResponse:
    content = f"""
      <div class="card status danger">
        <h1 style="margin-bottom:10px;">{html.escape(title)}</h1>
        <p>{html.escape(message)}</p>
        <a class="btn" href="/">Back home</a>
      </div>
    """
    return HTMLResponse(render_page(title, content), status_code=status_code, headers={"Cache-Control": "no-store"})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if wants_html(request):
        msg = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
        return render_error("Request failed", msg, exc.status_code)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if wants_html(request):
        return render_error("Invalid input", "Please check the form and try again.", 422)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.exception("Unhandled error: %s", exc)
    if wants_html(request):
        return render_error("Server error", "Unexpected error. Please try again.", 500)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def oauth_authorize_url(state: str) -> str:
    return (
        f"{DISCORD_API_BASE}/oauth2/authorize"
        f"?response_type=code&client_id={DISCORD_CLIENT_ID}"
        f"&scope={OAUTH_SCOPES}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&state={state}"
        f"&prompt=none"
    )


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def enforce_ip_rate_limit(ip: str):
    now = time.time()
    window_start = now - APPEAL_IP_WINDOW_SECONDS
    bucket = _ip_requests.setdefault(ip, [])
    bucket = [t for t in bucket if t >= window_start]
    if len(bucket) >= APPEAL_IP_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down and try again.")
    bucket.append(now)
    _ip_requests[ip] = bucket


async def exchange_code_for_token(code: str) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{DISCORD_API_BASE}/oauth2/token",
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": DISCORD_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        logging.warning("OAuth code exchange failed: %s | body=%s", exc, exc.response.text)
        raise HTTPException(status_code=400, detail="Authentication failed. Please try logging in again.") from exc


async def fetch_discord_user(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_ban_if_exists(user_id: str) -> Optional[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    return None


async def ensure_dm_guild_membership(user_id: str) -> bool:
    """Ensure we share a guild with the user so DMs can be delivered."""
    if not DM_GUILD_ID:
        return False
    if _declined_users.get(user_id):
        return False
    token = _user_tokens.get(user_id)
    if not token:
        return False
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"access_token": token},
        )
        added = resp.status_code in (200, 201, 204)
        if added and CLEANUP_DM_INVITES:
            try:
                invite_resp = await client.get(
                    f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/invites",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                )
                if invite_resp.status_code == 200:
                    for invite in invite_resp.json() or []:
                        code = invite.get("code")
                        if not code:
                            continue
                        await client.delete(
                            f"{DISCORD_API_BASE}/invites/{code}",
                            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                        )
                else:
                    logging.warning(
                        "Invite cleanup skipped status=%s body=%s",
                        invite_resp.status_code,
                        invite_resp.text,
                    )
            except Exception as exc:  # best-effort cleanup
                logging.exception("Failed invite cleanup: %s", exc)
    return added


async def maybe_remove_from_dm_guild(user_id: str):
    if not DM_GUILD_ID or not REMOVE_FROM_DM_GUILD_AFTER_DM:
        return
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )


async def remove_from_target_guild(user_id: str) -> Optional[int]:
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )
        if resp.status_code not in (200, 204, 404):
            logging.warning("Failed to remove user %s from guild %s: %s %s", user_id, TARGET_GUILD_ID, resp.status_code, resp.text)
        return resp.status_code


async def post_appeal_embed(
    appeal_id: str,
    user: dict,
    ban_reason: str,
    ban_evidence: str,
    appeal_reason: str,
) -> None:
    embed = {
        "title": f"Appeal #{appeal_id}",
        "color": 0x3498DB,
        "description": (
            f"**User:** <@{user['id']}> (`{user['username']}#{user.get('discriminator', '0')}`)\n"
            f"**Ban reason:** {ban_reason}\n"
            f"**Evidence:** {ban_evidence}\n"
            f"**Appeal:** {appeal_reason}"
        ),
        "footer": {"text": f"User ID: {user['id']}"},
    }
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Accept",
                    "custom_id": f"web_appeal_accept:{appeal_id}:{user['id']}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Decline",
                    "custom_id": f"web_appeal_decline:{appeal_id}:{user['id']}",
                },
            ],
        }
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{APPEAL_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed], "components": components},
        )
        resp.raise_for_status()


# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    state = serializer.dumps({"nonce": secrets.token_urlsafe(8)})
    user_session = read_user_session(request)
    history_html = ""
    if user_session and is_supabase_ready():
        history = await fetch_appeal_history(user_session["uid"])
        history_html = render_history_items(history)
    elif user_session:
        history_html = "<div class='muted'>History will appear after Supabase is configured.</div>"

    status_block = ""
    if user_session:
        uname = html.escape(user_session.get("uname", "Your account"))
        status_block = f"""
          <div class="card">
            <h2>Welcome back, {uname}</h2>
            <p class="muted">Track your BlockSpin ban appeals in one place. If you submit a new one, it will show below.</p>
            <div class="btn-row">
              <a class="btn" href="{oauth_authorize_url(state)}">Review my ban</a>
              <a class="btn secondary" href="/status">View live status</a>
            </div>
          </div>
        """

    history_block = f"""
      <div class="card">
        <h2>Appeal history</h2>
        <p class="muted">Logged with Supabase for transparency and security.</p>
        {history_html}
      </div>
    """
    fallback_status = f"""
      <div class="card">
        <h2>Stay signed in</h2>
        <p class="muted">Sign in once and we keep your session secured so you can check decisions anytime.</p>
        <div class="btn-row">
          <a class="btn" href="{oauth_authorize_url(state)}">Start now</a>
        </div>
      </div>
    """
    if not status_block:
        status_block = fallback_status

    content = f"""
      <div class="hero">
        <div>
          <div class="pill">BlockSpin Trust & Safety</div>
          <h1>Appeal, review, and monitor bans with confidence.</h1>
          <p class="lead">Modern portal for BlockSpin members to verify bans, submit one-time appeals, and stay updated with real-time status tracking.</p>
          <div class="btn-row">
            <a class="btn" href="{oauth_authorize_url(state)}">Login with Discord</a>
            <a class="btn secondary" href="#how-it-works">How it works</a>
          </div>
          <div class="timeline" id="how-it-works">
            <div class="step">Authenticate with Discord to verify you are appealing your own account.</div>
            <div class="step">Review ban details, share evidence, and submit a secure appeal.</div>
            <div class="step">Stay signed in to monitor decisions and download your history.</div>
          </div>
        </div>
        <div class="card">
          <h2>Why BlockSpin?</h2>
          <p class="muted">We pair Discord identity with server-side Supabase logging to keep appeals tamper-resistant.</p>
          <div class="features">
            <div class="feature"><strong>Live status</strong><span class="muted">Appeal outcomes mirrored to you and moderators instantly.</span></div>
            <div class="feature"><strong>Secure by default</strong><span class="muted">Strict rate limits, signed sessions, and audit trails.</span></div>
            <div class="feature"><strong>History</strong><span class="muted">See every appeal you have filed, including decisions.</span></div>
          </div>
        </div>
      </div>
      <div class="grid" style="margin-top:16px;">
        <div class="card">
          <h2>Appeal your ban</h2>
          <p class="muted">Sign in with your Discord account to view ban details and submit one appeal within 7 days.</p>
          <a class="btn" href="{oauth_authorize_url(state)}">Login with Discord</a>
          <div class="callout" style="margin-top:12px;">Built for BlockSpin. Keep your account secure and your appeal private.</div>
        </div>
        {status_block}
        {history_block}
      </div>
    """
    return HTMLResponse(render_page("BlockSpin Appeals", content), headers={"Cache-Control": "no-store"})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    session = read_user_session(request)
    if not session:
        login_url = oauth_authorize_url(serializer.dumps({"nonce": secrets.token_urlsafe(8)}))
        content = f"""
          <div class="card status danger">
            <h1 style="margin-bottom:10px;">Sign in required</h1>
            <p class="muted">Sign in to view your BlockSpin appeal history and live status.</p>
            <a class="btn" href="{login_url}">Login with Discord</a>
          </div>
        """
        return HTMLResponse(render_page("Appeal status", content), status_code=401, headers={"Cache-Control": "no-store"})

    history_html = ""
    if is_supabase_ready():
        history = await fetch_appeal_history(session["uid"], limit=10)
        history_html = render_history_items(history)
    else:
        history_html = "<div class='muted'>History will appear after Supabase is configured.</div>"

    content = f"""
      <div class="card">
        <div class="pill">BlockSpin | Appeal status</div>
        <h1 style="margin:12px 0 8px;">Appeal history for {html.escape(session.get('uname','you'))}</h1>
        <p class="muted">You are signed in. We keep this session encrypted for {PERSIST_SESSION_SECONDS // 86400} days.</p>
        {history_html}
        <div class="footer">Need to update details? Start a new session from the home page.</div>
      </div>
    """
    return HTMLResponse(render_page("Appeal status", content), headers={"Cache-Control": "no-store"})


@app.get("/callback")
async def callback(request: Request, code: str, state: str):
    try:
        serializer.loads(state)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid state")

    token = await exchange_code_for_token(code)
    user = await fetch_discord_user(token["access_token"])
    _user_tokens[user["id"]] = token["access_token"]
    uname_label = f"{user['username']}#{user.get('discriminator', '0')}"

    history_html = ""
    if is_supabase_ready():
        history = await fetch_appeal_history(user["id"])
        history_html = render_history_items(history)
    else:
        history_html = "<div class='muted'>History will appear after Supabase is configured.</div>"

    def respond(body_html: str, title: str, status_code: int = 200) -> HTMLResponse:
        resp = HTMLResponse(render_page(title, body_html), status_code=status_code, headers={"Cache-Control": "no-store"})
        persist_user_session(resp, user["id"], uname_label)
        return resp

    # Block re-entry for declined users up front.
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
    window_remaining = int(max(0, window_expires_at - now))
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

    # Only now join the DM guild (best effort) and tidy up invites.
    await ensure_dm_guild_membership(user["id"])

    session = serializer.dumps(
        {
            "uid": user["id"],
            "uname": f"{user['username']}#{user.get('discriminator','0')}",
            "ban_reason": ban.get("reason", "No reason provided."),
            "iat": time.time(),
            "ban_first_seen": first_seen,
        }
    )
    uname = html.escape(f"{user['username']}#{user.get('discriminator','0')}")
    ban_reason = html.escape(ban.get("reason", "No reason provided."))
    cooldown_minutes = max(1, APPEAL_COOLDOWN_SECONDS // 60)
    content = f"""
      <div class="grid-2">
        <div class="form-card">
          <div class="badge">Window: {max(1, window_remaining // 60)} minutes left</div>
          <h2 style="margin:8px 0;">Appeal your BlockSpin ban</h2>
          <p class="muted">One appeal per ban. Include context, evidence, and what you will change.</p>
          <form class="form" action="/submit" method="post">
            <input type="hidden" name="session" value="{session}" />
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
          <div class="callout" style="margin-top:10px;">We keep you signed in so you can check your appeal status without re-authenticating.</div>
        </div>
        <div class="card">
          <h2>Ban details</h2>
          <p class="muted"><strong>User:</strong> {uname}</p>
          <p class="muted"><strong>Ban reason:</strong> {ban_reason}</p>
          <p class="muted">Cooldown between submissions: {cooldown_minutes} minutes. Appeals expire 7 days after the ban.</p>
          <div style="margin-top:12px;">
            <h3 style="margin:0 0 6px;">Your history</h3>
            {history_html}
          </div>
        </div>
      </div>
    """
    return respond(content, "Appeal your ban", 200)


@app.post("/submit")
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

    # Session expiry + single-use guard
    issued_at = float(data.get("iat", 0))
    if not issued_at or now - issued_at > SESSION_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="This form session expired. Please restart the appeal.")
    if session in _used_sessions:
        raise HTTPException(status_code=409, detail="This appeal was already submitted.")

    first_seen = float(data.get("ban_first_seen", now))
    if now - first_seen > APPEAL_WINDOW_SECONDS:
        raise HTTPException(status_code=403, detail="This ban is older than the appeal window.")

    # Per-IP throttle to slow basic spam
    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    user_agent = request.headers.get("User-Agent", "unknown")
    enforce_ip_rate_limit(ip)

    # Rate limit to prevent spam
    last = _appeal_rate_limit.get(data["uid"])
    if last and now - last < APPEAL_COOLDOWN_SECONDS:
        wait = int(APPEAL_COOLDOWN_SECONDS - (now - last))
        raise HTTPException(status_code=429, detail=f"Please wait {wait} seconds before submitting another appeal.")
    _appeal_rate_limit[data["uid"]] = now

    appeal_id = str(uuid.uuid4())[:8]
    user = {"id": data["uid"], "username": data["uname"], "discriminator": "0"}
    await post_appeal_embed(
        appeal_id=appeal_id,
        user=user,
        ban_reason=data.get("ban_reason", "No reason provided."),
        ban_evidence=evidence or "No evidence provided.",
        appeal_reason=appeal_reason,
    )

    # Persist audit trail to Supabase (best effort)
    await log_appeal_to_supabase(
        appeal_id,
        user,
        data.get("ban_reason", "No reason provided."),
        evidence or "No evidence provided.",
        appeal_reason,
        ip,
        forwarded_for,
        user_agent,
    )

    _used_sessions[session] = now
    _appeal_locked[data["uid"]] = True
    # prune old used sessions
    stale_sessions = [token for token, ts in _used_sessions.items() if now - ts > SESSION_TTL_SECONDS * 2]
    for token in stale_sessions:
        _used_sessions.pop(token, None)

    success = f"""
      <div class="card">
        <h1>Appeal submitted</h1>
        <p>Reference ID: <strong>{appeal_id}</strong></p>
        <p class="muted">We will review your appeal shortly. You will be notified in Discord.</p>
        <a class="btn" href="/">Back home</a>
      </div>
    """

    return HTMLResponse(render_page("Appeal submitted", success), status_code=200, headers={"Cache-Control": "no-store"})


# --- Discord interactions (button handling) ---
def verify_signature(request: Request, body: bytes) -> bool:
    import nacl.signing
    import nacl.exceptions

    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    if not signature or not timestamp:
        return False
    try:
        key = nacl.signing.VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        key.verify(f"{timestamp}".encode() + body, bytes.fromhex(signature))
        return True
    except (ValueError, nacl.exceptions.BadSignatureError):
        return False


async def respond_ephemeral(content: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
            "data": {"content": content, "flags": 1 << 6},
        }
    )


async def respond_ephemeral_embed(title: str, description: str, color: int = 0xE67E22) -> JSONResponse:
    return JSONResponse(
        {
            "type": 4,
            "data": {
                "flags": 1 << 6,
                "embeds": [{"title": title, "description": description, "color": color}],
            },
        }
    )


async def edit_original(message: dict, content: Optional[str] = None, color: int = 0x2ecc71):
    embeds = message.get("embeds") or []
    if embeds:
        embeds[0]["color"] = color
        embeds[0]["fields"] = embeds[0].get("fields", []) + [
            {"name": "Decision", "value": content or "Updated", "inline": False}
        ]
    return {"type": 7, "data": {"embeds": embeds, "components": []}}


async def dm_user(user_id: str, embed: dict):
    # Ensure we share a guild for DMs; this is best-effort.
    await ensure_dm_guild_membership(user_id)
    async with httpx.AsyncClient() as client:
        dm = await client.post(
            f"{DISCORD_API_BASE}/users/@me/channels",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"recipient_id": user_id},
        )
        if dm.status_code not in (200, 201):
            return False
        channel_id = dm.json().get("id")
        if not channel_id:
            return False
        resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed]},
        )
        delivered = resp.status_code in (200, 201)
        if delivered:
            await maybe_remove_from_dm_guild(user_id)
        return delivered


def build_decision_embed(
    status: str,
    appeal_id: str,
    user_id: str,
    moderator_id: str,
    dm_delivered: bool,
    unban_status: Optional[int] = None,
    removal_status: Optional[int] = None,
    invite_link: Optional[str] = None,
):
    accepted = status == "accepted"
    color = 0x2ECC71 if accepted else 0xE74C3C
    title = "Appeal Accepted" if accepted else "Appeal Declined"
    desc = f"Appeal `{appeal_id}` {title.lower()}.\nUser: <@{user_id}>\nModerator: <@{moderator_id}>"
    if invite_link and accepted:
        desc += f"\nInvite: {invite_link}"
    fields = [
        {"name": "DM delivery", "value": "Delivered" if dm_delivered else "Failed", "inline": True},
    ]
    if unban_status:
        fields.append({"name": "Unban", "value": str(unban_status), "inline": True})
    if removal_status:
        fields.append({"name": "Guild removal", "value": str(removal_status), "inline": True})
    return {
        "title": title,
        "description": desc,
        "color": color,
        "fields": fields,
    }


@app.post("/interactions")
async def interactions(request: Request):
    body = await request.body()
    logging.info("Interactions: received request, bytes=%s", len(body))
    if not verify_signature(request, body):
        logging.warning("Interactions: invalid signature")
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    payload = await request.json()
    logging.info("Interactions: payload type=%s id=%s", payload.get("type"), payload.get("id"))
    if payload["type"] == 1:  # PING
        return JSONResponse({"type": 1})

    if payload["type"] == 3:  # COMPONENT
        data = payload.get("data", {})
        custom_id = data.get("custom_id", "")
        member = payload.get("member") or {}
        user_obj = member.get("user") or {}
        moderator_id = user_obj.get("id")
        logging.info(
            "Interactions: component custom_id=%s moderator=%s channel=%s message=%s",
            custom_id,
            moderator_id,
            payload.get("channel_id"),
            payload.get("message", {}).get("id"),
        )

        # prune old processed appeals to avoid unbounded growth
        now = time.time()
        for k in list(_processed_appeals.keys()):
            if now - _processed_appeals[k] > 3600:
                _processed_appeals.pop(k, None)

        # Check permissions
        roles = set(map(int, member.get("roles", [])))
        if MODERATOR_ROLE_ID not in roles:
            return await respond_ephemeral_embed(
                "Not allowed",
                "You don't have the moderator role required to handle appeals.",
                0xE74C3C,
            )

        try:
            action, appeal_id, user_id = custom_id.split(":")
        except ValueError:
            logging.warning("Interactions: malformed custom_id=%s", custom_id)
            return await respond_ephemeral_embed("Invalid request", "Bad interaction payload.")

        # Extract message details for editing later
        channel_id = payload["channel_id"]
        message_id = payload["message"]["id"]
        embeds = payload["message"].get("embeds") or []
        original_embed = copy.deepcopy(embeds[0]) if embeds else {}

        # Basic replay/spam guard: ignore if custom_id format looks wrong or missing ids
        if not appeal_id or not user_id or action not in {"web_appeal_accept", "web_appeal_decline"}:
            return await respond_ephemeral_embed("Invalid request", "Malformed interaction data.")

        # Prepare immediate UI update embed (buttons removed)
        def updated_embed(status: str) -> dict:
            embed = copy.deepcopy(original_embed) or {}
            if status == "accepted":
                color = 0x2ECC71
                suffix = " (ACCEPTED)"
                label = "Accepted"
            else:
                color = 0xE74C3C
                suffix = " (DECLINED)"
                label = "Declined"
            embed["color"] = color
            embed["title"] = embed.get("title", "Appeal") + suffix
            embed["fields"] = embed.get("fields", []) + [
                {"name": "Action Taken", "value": f"{label} by <@{moderator_id}>", "inline": False}
            ]
            return embed

        # Run the heavy work in background to avoid interaction timeouts.
        async def handle_accept():
            try:
                # idempotency: ignore double clicks / retries
                if appeal_id in _processed_appeals:
                    logging.info("Interactions: appeal %s already processed, skipping accept", appeal_id)
                    return
                _processed_appeals[appeal_id] = time.time()

                unban_status = None
                async with httpx.AsyncClient() as client:
                    unban_resp = await client.delete(
                        f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    )
                    unban_status = unban_resp.status_code

                removal_status = await remove_from_target_guild(user_id)

                # DM user (best effort)
                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Accepted",
                        "description": (
                            "Your appeal has been reviewed and accepted. You have been unbanned.\n"
                            f"Use this invite to rejoin BlockSpin: {INVITE_LINK}"
                        ),
                        "color": 0x2ECC71,
                    },
                )

                await update_appeal_status(
                    appeal_id=appeal_id,
                    status="accepted",
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    notes=f"unban:{unban_status} removal:{removal_status}",
                )

                log_embed = build_decision_embed(
                    status="accepted",
                    appeal_id=appeal_id,
                    user_id=user_id,
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    unban_status=unban_status,
                    removal_status=removal_status,
                    invite_link=INVITE_LINK,
                )

                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                        json={"embeds": [log_embed]},
                    )
            except Exception as exc:  # log for debugging
                logging.exception("Failed to process acceptance for appeal %s: %s", appeal_id, exc)

        async def handle_decline():
            try:
                if appeal_id in _processed_appeals:
                    logging.info("Interactions: appeal %s already processed, skipping decline", appeal_id)
                    return
                _processed_appeals[appeal_id] = time.time()
                _declined_users[user_id] = True
                _appeal_locked[user_id] = True

                removal_status = await remove_from_target_guild(user_id)

                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Declined",
                        "description": (
                            "Your appeal has been reviewed and declined. Further appeals are blocked for this ban.\n"
                            "You have been removed from the guild for security."
                        ),
                        "color": 0xE74C3C,
                    },
                )

                await update_appeal_status(
                    appeal_id=appeal_id,
                    status="declined",
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    notes=f"declined removal:{removal_status}",
                )

                log_embed = build_decision_embed(
                    status="declined",
                    appeal_id=appeal_id,
                    user_id=user_id,
                    moderator_id=moderator_id,
                    dm_delivered=dm_delivered,
                    removal_status=removal_status,
                )

                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                        json={"embeds": [log_embed]},
                    )

                # Remove from DM guild after decline and cleanup invites
                await maybe_remove_from_dm_guild(user_id)
            except Exception as exc:  # log for debugging
                logging.exception("Failed to process decline for appeal %s: %s", appeal_id, exc)

        if action == "web_appeal_accept":
            asyncio.create_task(handle_accept())
            return JSONResponse(
                {
                    "type": 7,
                    "data": {"embeds": [updated_embed("accepted")], "components": []},
                }
            )

        if action == "web_appeal_decline":
            asyncio.create_task(handle_decline())
            return JSONResponse(
                {
                    "type": 7,
                    "data": {"embeds": [updated_embed("declined")], "components": []},
                }
            )

    return JSONResponse({"type": 4, "data": {"content": "Unsupported interaction", "flags": 1 << 6}})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
