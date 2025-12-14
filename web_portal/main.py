import os
import secrets
import uuid
import asyncio
import time
import html
import copy
import hashlib
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from typing import Optional, Tuple, Dict, List, Any, Union

try:
    import discord  # type: ignore
except ImportError:  # allow app to boot even if discord.py isn't installed
    discord = None

import httpx
from jinja2 import Environment, select_autoescape
from fastapi import FastAPI, Form, HTTPException, Request, Depends, Response
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
AUTH_LOG_CHANNEL_ID = int(os.getenv("AUTH_LOG_CHANNEL_ID", "1449822248490762421"))
SECRET_KEY = os.getenv("PORTAL_SECRET_KEY") or secrets.token_hex(16)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "discord-appeals"
SUPABASE_SESSION_TABLE = "discord-appeal-sessions"
SUPABASE_CONTEXT_TABLE = "banned_user_context"
INVITE_LINK = "https://discord.gg/blockspin"
MESSAGE_CACHE_GUILD_IDS_RAW = os.getenv("MESSAGE_CACHE_GUILD_ID", "1337420081382297682")
READD_GUILD_ID = os.getenv("READD_GUILD_ID", "1065973360040890418")
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate")
DEBUG_EVENTS = os.getenv("DEBUG_EVENTS", "false").lower() == "true"

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

# --- Shared resources ---
http_client: Optional[httpx.AsyncClient] = None
_temp_http_client: Optional[httpx.AsyncClient] = None
JINJA_ENV = Environment(autoescape=select_autoescape(default_for_string=True, default=True))

async def app_lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    try:
        yield
    finally:
        if http_client:
            await http_client.aclose()
            http_client = None
        if _temp_http_client:
            await _temp_http_client.aclose()
            _temp_http_client = None


def get_http_client() -> httpx.AsyncClient:
    if http_client:
        return http_client
    global _temp_http_client
    if not _temp_http_client:
        _temp_http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _temp_http_client

# --- Basic app setup ---
app = FastAPI(title="BlockSpin Appeals Portal", lifespan=app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logging.basicConfig(level=logging.INFO)
serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")

@app.on_event("startup")
async def startup_event():
    if not bot_client:
        logging.warning("discord.py not available; bot client not started.")
        raise RuntimeError("discord.py is required for the appeal bot. Please install dependencies.")
    if not DISCORD_BOT_TOKEN:
        logging.warning("DISCORD_BOT_TOKEN missing; bot client not started.")
        raise RuntimeError("DISCORD_BOT_TOKEN missing; bot client cannot start.")
    try:
        await bot_client.login(DISCORD_BOT_TOKEN)
    except Exception as exc:
        logging.exception("Discord bot login failed: %s", exc)
        raise RuntimeError("Discord bot token is invalid or missing required intents.") from exc
    asyncio.create_task(bot_client.connect())

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response

# --- In-memory stores with improved structure ---
_appeal_rate_limit: Dict[str, float] = {}  # {user_id: timestamp_of_last_submit}
_used_sessions: Dict[str, float] = {}  # {session_token: timestamp_used}
_ip_requests: Dict[str, List[float]] = {}  # {ip: [timestamps]}
_ban_first_seen: Dict[str, float] = {}  # {user_id: first time we saw the ban}
_appeal_locked: Dict[str, bool] = {}  # {user_id: True if appealed already}
_user_tokens: Dict[str, Dict[str, Any]] = {}  # {user_id: {"access_token": str, "refresh_token": str, "expires_at": float}}
_processed_appeals: Dict[str, float] = {}  # {appeal_id: timestamp_processed}
_declined_users: Dict[str, bool] = {}  # {user_id: True if appeal declined}
_state_tokens: Dict[str, Tuple[str, float]] = {}  # {token: (ip, issued_at)}

# --- Configuration variables with improved defaults ---
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

# --- Bot & Cache Setup with improved message caching ---
bot_client = None
_message_buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=15))  # Store last 15 messages per user
_recent_message_context: Dict[str, Tuple[List[dict], float]] = {}  # Cache of recent messages with timestamp
RECENT_MESSAGE_CACHE_TTL = int(os.getenv("RECENT_MESSAGE_CACHE_TTL", "120"))  # 2 minutes

MESSAGE_CACHE_GUILD_IDS = {
    gid.strip()
    for gid in MESSAGE_CACHE_GUILD_IDS_RAW.split(",")
    if gid.strip()
}
if not MESSAGE_CACHE_GUILD_IDS:
    MESSAGE_CACHE_GUILD_IDS = None

def uid(value: Any) -> str:
    return str(value)

def should_track_messages(guild_id: int) -> bool:
    if MESSAGE_CACHE_GUILD_IDS is None:
        return True
    return str(guild_id) in MESSAGE_CACHE_GUILD_IDS

# --- Improved UI/UX Styles ---
BASE_STYLES = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --bg: #0a0b0f;
  --bg-secondary: #0f1118;
  --bg-tertiary: #151923;
  --card: rgba(20, 24, 33, 0.85);
  --card-hover: rgba(25, 30, 40, 0.9);
  --border: rgba(255, 255, 255, 0.08);
  --border-hover: rgba(255, 255, 255, 0.15);
  
  --text-primary: #ffffff;
  --text-secondary: #a0aec0;
  --text-muted: #718096;
  
  --accent: #5b6fee;
  --accent-hover: #4c5fd9;
  --accent-light: rgba(91, 111, 238, 0.15);
  
  --success: #48bb78;
  --success-light: rgba(72, 187, 120, 0.15);
  
  --warning: #ed8936;
  --warning-light: rgba(237, 137, 54, 0.15);
  
  --danger: #f56565;
  --danger-light: rgba(245, 101, 101, 0.15);
  
  --radius: 12px;
  --radius-lg: 16px;
  
  --shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
  --shadow-lg: 0 20px 40px rgba(0, 0, 0, 0.4);
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html, body {
  height: 100%;
  font-family: "Inter", system-ui, -apple-system, sans-serif;
  color: var(--text-primary);
  background: linear-gradient(135deg, var(--bg) 0%, var(--bg-secondary) 100%);
}

body {
  line-height: 1.6;
  font-size: 16px;
  overflow-x: hidden;
}

.app {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  padding: 20px;
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
}

/* --- Header --- */
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 30px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border);
}

.brand {
  display: flex;
  align-items: center;
  gap: 15px;
}

.logo {
  width: 50px;
  height: 50px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 20px;
  background: linear-gradient(135deg, var(--accent), #7c3aed);
  box-shadow: 0 8px 20px rgba(91, 111, 238, 0.25);
  color: white;
}

.brand-text h1 {
  font-size: 24px;
  font-weight: 700;
  margin-bottom: 4px;
}

.brand-text span {
  font-size: 14px;
  color: var(--text-secondary);
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: var(--card);
  border-radius: 100px;
  border: 1px solid var(--border);
}

.user-chip img {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  object-fit: cover;
  border: 2px solid var(--accent);
}

.user-chip .name {
  font-weight: 600;
  font-size: 15px;
}

.user-chip .actions a {
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 14px;
  margin-left: 10px;
  transition: color 0.2s;
}

.user-chip .actions a:hover {
  color: var(--text-primary);
}

/* --- Main Content --- */
.content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 25px;
}

.card {
  background: var(--card);
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
  padding: 30px;
  box-shadow: var(--shadow);
  transition: transform 0.2s, box-shadow 0.2s;
}

.card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}

.hero {
  background: linear-gradient(135deg, var(--card) 0%, var(--card-hover) 100%);
  border: 1px solid var(--accent-light);
  padding: 40px;
  text-align: center;
}

.hero h1 {
  font-size: 32px;
  font-weight: 700;
  margin-bottom: 15px;
  background: linear-gradient(135deg, var(--text-primary) 0%, var(--accent) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.hero p {
  font-size: 18px;
  color: var(--text-secondary);
  margin-bottom: 30px;
  max-width: 700px;
  margin-left: auto;
  margin-right: auto;
}

.status-indicator {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 100px;
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 20px;
}

.status-indicator.pending {
  background: var(--warning-light);
  color: var(--warning);
}

.status-indicator.approved {
  background: var(--success-light);
  color: var(--success);
}

.status-indicator.declined {
  background: var(--danger-light);
  color: var(--danger);
}

/* --- Buttons --- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 24px;
  border-radius: var(--radius);
  font-weight: 600;
  font-size: 16px;
  text-decoration: none;
  cursor: pointer;
  transition: all 0.2s;
  border: none;
  outline: none;
}

.btn-primary {
  background: linear-gradient(135deg, var(--accent) 0%, #4c5fd9 100%);
  color: white;
  box-shadow: 0 4px 15px rgba(91, 111, 238, 0.25);
}

.btn-primary:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(91, 111, 238, 0.35);
}

.btn-secondary {
  background: var(--bg-tertiary);
  color: var(--text-primary);
  border: 1px solid var(--border);
}

.btn-secondary:hover {
  background: var(--border);
  transform: translateY(-1px);
}

.btn-danger {
  background: var(--danger);
  color: white;
}

.btn-danger:hover {
  background: #e53e3e;
  transform: translateY(-1px);
}

.btn-group {
  display: flex;
  gap: 15px;
  flex-wrap: wrap;
  justify-content: center;
  margin-top: 25px;
}

/* --- Forms --- */
.form-group {
  margin-bottom: 20px;
}

.form-group label {
  display: block;
  margin-bottom: 8px;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.form-control {
  width: 100%;
  padding: 14px 16px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--bg-tertiary);
  color: var(--text-primary);
  font-size: 16px;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.form-control:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-light);
}

textarea.form-control {
  min-height: 150px;
  resize: vertical;
  font-family: inherit;
}

/* --- Grid Layout --- */
.grid {
  display: grid;
  gap: 25px;
}

.grid-2 {
  grid-template-columns: 1fr 1fr;
}

@media (max-width: 768px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
  
  .app {
    padding: 15px;
  }
  
  .hero {
    padding: 30px 20px;
  }
  
  .hero h1 {
    font-size: 26px;
  }
  
  .hero p {
    font-size: 16px;
  }
  
  .btn-group {
    flex-direction: column;
  }
  
  .btn {
    width: 100%;
  }
}

/* --- Steps / Process --- */
.steps {
  display: flex;
  justify-content: space-between;
  margin: 40px 0;
  position: relative;
}

.steps::before {
  content: '';
  position: absolute;
  top: 25px;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--border);
  z-index: 1;
}

.step {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  width: 150px;
  position: relative;
  z-index: 2;
}

.step-number {
  width: 50px;
  height: 50px;
  border-radius: 50%;
  background: var(--bg-tertiary);
  border: 2px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 20px;
  margin-bottom: 15px;
}

.step.active .step-number {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
  box-shadow: 0 0 15px rgba(91, 111, 238, 0.4);
}

.step-title {
  font-weight: 600;
  margin-bottom: 8px;
  font-size: 16px;
}

.step-description {
  font-size: 14px;
  color: var(--text-secondary);
  line-height: 1.5;
}

/* --- Message Context --- */
.message-context {
  max-height: 400px;
  overflow-y: auto;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--bg-tertiary);
  padding: 15px;
}

.message {
  padding: 15px;
  border-radius: var(--radius);
  margin-bottom: 15px;
  background: var(--card);
  border: 1px solid var(--border);
}

.message:last-child {
  margin-bottom: 0;
}

.message-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 10px;
}

.message-author {
  font-weight: 600;
  color: var(--accent);
}

.message-time {
  font-size: 13px;
  color: var(--text-muted);
}

.message-channel {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 100px;
  font-size: 12px;
  font-weight: 600;
  background: var(--accent-light);
  color: var(--accent);
  margin-bottom: 8px;
}

.message-content {
  font-size: 15px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

/* --- Appeal History --- */
.appeal-history {
  margin-top: 30px;
}

.appeal-item {
  padding: 20px;
  border-radius: var(--radius);
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  margin-bottom: 15px;
}

.appeal-item:last-child {
  margin-bottom: 0;
}

.appeal-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 15px;
}

.appeal-id {
  font-weight: 600;
  color: var(--text-secondary);
}

.appeal-date {
  font-size: 14px;
  color: var(--text-muted);
}

.appeal-status {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 100px;
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
}

.appeal-status.pending {
  background: var(--warning-light);
  color: var(--warning);
}

.appeal-status.approved {
  background: var(--success-light);
  color: var(--success);
}

.appeal-status.declined {
  background: var(--danger-light);
  color: var(--danger);
}

.appeal-details {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
}

.detail-item {
  display: flex;
  flex-direction: column;
}

.detail-label {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 5px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.detail-value {
  font-size: 15px;
}

/* --- Alerts / Notifications --- */
.alert {
  padding: 15px 20px;
  border-radius: var(--radius);
  margin-bottom: 20px;
  font-weight: 500;
}

.alert-success {
  background: var(--success-light);
  color: var(--success);
  border: 1px solid rgba(72, 187, 120, 0.3);
}

.alert-warning {
  background: var(--warning-light);
  color: var(--warning);
  border: 1px solid rgba(237, 137, 54, 0.3);
}

.alert-danger {
  background: var(--danger-light);
  color: var(--danger);
  border: 1px solid rgba(245, 101, 101, 0.3);
}

.alert-info {
  background: var(--accent-light);
  color: var(--accent);
  border: 1px solid rgba(91, 111, 238, 0.3);
}

/* --- Footer --- */
.footer {
  margin-top: 40px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}

.footer a {
  color: var(--text-secondary);
  text-decoration: none;
  transition: color 0.2s;
}

.footer a:hover {
  color: var(--text-primary);
}

/* --- Loading / Spinner --- */
.spinner {
  display: inline-block;
  width: 20px;
  height: 20px;
  border: 3px solid rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  border-top-color: white;
  animation: spin 1s ease-in-out infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* --- Animations --- */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.fade-in {
  animation: fadeIn 0.5s ease-out;
}

/* --- Accessibility --- */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}

/* --- Focus Styles --- */
.btn:focus,
.form-control:focus {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
"""

# --- Language Support with improved strings ---
LANG_STRINGS = {
    "en": {
        "site_title": "BlockSpin Appeals Portal",
        "hero_title": "Discord Ban Appeals",
        "hero_subtitle": "Submit an appeal for your account ban and track its status",
        "login_button": "Login with Discord",
        "how_it_works": "How It Works",
        "step_1_title": "Authenticate",
        "step_1_desc": "Login with your Discord account to verify your identity",
        "step_2_title": "Review & Submit",
        "step_2_desc": "Review your ban details and submit your appeal with evidence",
        "step_3_title": "Track Status",
        "step_3_desc": "Monitor your appeal status and receive notifications about decisions",
        "appeal_button": "Submit Appeal",
        "status_button": "Check Status",
        "welcome_back": "Welcome back",
        "review_ban": "Review Ban Details",
        "logout": "Logout",
        "error_title": "Error",
        "error_message": "An error occurred while processing your request",
        "retry_button": "Try Again",
        "home_button": "Go Home",
        "ban_details": "Ban Information",
        "ban_reason": "Ban Reason",
        "ban_date": "Ban Date",
        "appeal_reason": "Appeal Reason",
        "additional_info": "Additional Information",
        "submit_appeal": "Submit Appeal",
        "appeal_history": "Appeal History",
        "no_appeals": "No appeals found",
        "status_pending": "Pending",
        "status_approved": "Approved",
        "status_declined": "Declined",
        "message_context": "Recent Messages",
        "no_messages": "No recent messages available",
        "language_switch": "Language",
        "footer_copyright": "© {year} BlockSpin Community. All rights reserved.",
        "footer_privacy": "Privacy Policy",
        "footer_terms": "Terms of Service",
    },
    "es": {
        "site_title": "Portal de Apelaciones de BlockSpin",
        "hero_title": "Apelaciones de Baneo de Discord",
        "hero_subtitle": "Envía una apelación para tu baneo de cuenta y sigue su estado",
        "login_button": "Iniciar sesión con Discord",
        "how_it_works": "Cómo Funciona",
        "step_1_title": "Autenticar",
        "step_1_desc": "Inicia sesión con tu cuenta de Discord para verificar tu identidad",
        "step_2_title": "Revisar y Enviar",
        "step_2_desc": "Revisa los detalles de tu baneo y envía tu apelación con evidencia",
        "step_3_title": "Seguir Estado",
        "step_3_desc": "Monitorea el estado de tu apelación y recibe notificaciones sobre decisiones",
        "appeal_button": "Enviar Apelación",
        "status_button": "Ver Estado",
        "welcome_back": "Bienvenido de nuevo",
        "review_ban": "Revisar Detalles del Baneo",
        "logout": "Cerrar sesión",
        "error_title": "Error",
        "error_message": "Ocurrió un error al procesar tu solicitud",
        "retry_button": "Intentar de nuevo",
        "home_button": "Ir al inicio",
        "ban_details": "Información del Baneo",
        "ban_reason": "Razón del Baneo",
        "ban_date": "Fecha del Baneo",
        "appeal_reason": "Razón de la Apelación",
        "additional_info": "Información Adicional",
        "submit_appeal": "Enviar Apelación",
        "appeal_history": "Historial de Apelaciones",
        "no_appeals": "No se encontraron apelaciones",
        "status_pending": "Pendiente",
        "status_approved": "Aprobada",
        "status_declined": "Rechazada",
        "message_context": "Mensajes Recientes",
        "no_messages": "No hay mensajes recientes disponibles",
        "language_switch": "Idioma",
        "footer_copyright": "© {year} Comunidad BlockSpin. Todos los derechos reservados.",
        "footer_privacy": "Política de Privacidad",
        "footer_terms": "Términos de Servicio",
    },
}

# --- Improved Templates with better UX ---
HOME_TEMPLATE = """
<div class="content">
  <div class="card hero fade-in">
    <h1>{{ strings["hero_title"] }}</h1>
    <p>{{ strings["hero_subtitle"] }}</p>
    
    <div class="btn-group">
      <a href="/login" class="btn btn-primary">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
        </svg>
        {{ strings["login_button"] }}
      </a>
    </div>
  </div>
  
  <div class="card fade-in">
    <h2>{{ strings["how_it_works"] }}</h2>
    
    <div class="steps">
      <div class="step active">
        <div class="step-number">1</div>
        <div class="step-title">{{ strings["step_1_title"] }}</div>
        <div class="step-description">{{ strings["step_1_desc"] }}</div>
      </div>
      
      <div class="step active">
        <div class="step-number">2</div>
        <div class="step-title">{{ strings["step_2_title"] }}</div>
        <div class="step-description">{{ strings["step_2_desc"] }}</div>
      </div>
      
      <div class="step active">
        <div class="step-number">3</div>
        <div class="step-title">{{ strings["step_3_title"] }}</div>
        <div class="step-description">{{ strings["step_3_desc"] }}</div>
      </div>
    </div>
  </div>
</div>
"""

APPEAL_TEMPLATE = """
<div class="content">
  <div class="card fade-in">
    <div class="status-indicator pending">Ban Active</div>
    <h2>{{ strings["ban_details"] }}</h2>
    
    <div class="grid grid-2">
      <div class="form-group">
        <label>{{ strings["ban_reason"] }}</label>
        <div class="form-control" style="background: var(--bg-tertiary); padding: 14px;">{{ ban_reason or "No reason provided" }}</div>
      </div>
      
      <div class="form-group">
        <label>{{ strings["ban_date"] }}</label>
        <div class="form-control" style="background: var(--bg-tertiary); padding: 14px;">{{ ban_date or "Unknown" }}</div>
      </div>
    </div>
    
    <form method="post" action="/submit-appeal">
      <div class="form-group">
        <label for="appeal_reason">{{ strings["appeal_reason"] }} *</label>
        <textarea id="appeal_reason" name="appeal_reason" class="form-control" required placeholder="Explain why you believe your ban should be lifted..."></textarea>
      </div>
      
      <div class="form-group">
        <label for="additional_info">{{ strings["additional_info"] }}</label>
        <textarea id="additional_info" name="additional_info" class="form-control" placeholder="Any additional information or evidence you'd like to provide..."></textarea>
      </div>
      
      <div class="btn-group">
        <button type="submit" class="btn btn-primary">{{ strings["submit_appeal"] }}</button>
        <a href="/status" class="btn btn-secondary">{{ strings["status_button"] }}</a>
      </div>
    </form>
  </div>
  
  {% if messages %}
  <div class="card fade-in">
    <h2>{{ strings["message_context"] }}</h2>
    <div class="message-context">
      {% for message in messages %}
      <div class="message">
        <div class="message-header">
          <div class="message-author">{{ message.author or "You" }}</div>
          <div class="message-time">{{ message.timestamp or "Unknown time" }}</div>
        </div>
        <div class="message-channel">#{{ message.channel_name or "unknown" }}</div>
        <div class="message-content">{{ message.content }}</div>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
</div>
"""

STATUS_TEMPLATE = """
<div class="content">
  <div class="card fade-in">
    <h2>{{ strings["appeal_history"] }}</h2>
    
    {% if appeals %}
    <div class="appeal-history">
      {% for appeal in appeals %}
      <div class="appeal-item">
        <div class="appeal-header">
          <div class="appeal-id">ID: {{ appeal.appeal_id }}</div>
          <div class="appeal-date">{{ appeal.created_at }}</div>
        </div>
        
        <div class="appeal-status {{ appeal.status }}">{{ appeal.status | title }}</div>
        
        <div class="appeal-details">
          <div class="detail-item">
            <div class="detail-label">{{ strings["ban_reason"] }}</div>
            <div class="detail-value">{{ appeal.ban_reason or "Not specified" }}</div>
          </div>
          
          <div class="detail-item">
            <div class="detail-label">{{ strings["appeal_reason"] }}</div>
            <div class="detail-value">{{ appeal.appeal_reason or "Not specified" }}</div>
          </div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="alert alert-info">{{ strings["no_appeals"] }}</div>
    {% endif %}
    
    <div class="btn-group">
      {% if can_appeal %}
      <a href="/appeal" class="btn btn-primary">{{ strings["appeal_button"] }}</a>
      {% endif %}
      <a href="/" class="btn btn-secondary">{{ strings["home_button"] }}</a>
    </div>
  </div>
</div>
"""

ERROR_TEMPLATE = """
<div class="content">
  <div class="card fade-in">
    <div class="status-indicator declined">Error</div>
    <h2>{{ strings["error_title"] }}</h2>
    <p>{{ message }}</p>
    
    <div class="btn-group">
      <a href="{{ retry_url or '/' }}" class="btn btn-primary">{{ strings["retry_button"] }}</a>
      <a href="/" class="btn btn-secondary">{{ strings["home_button"] }}</a>
    </div>
  </div>
</div>
"""

# --- Improved Bot Implementation with Fixed Message Caching ---
if discord:
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.members = True
    intents.bans = True
    intents.guilds = True

    bot_client = discord.Client(intents=intents)

    @bot_client.event
    async def on_ready():
        logging.info("Bot connected as %s (%s)", bot_client.user, getattr(bot_client.user, "id", "unknown"))
        # Initialize message buffer for all guilds if needed
        if MESSAGE_CACHE_GUILD_IDS:
            logging.info("Message caching enabled for guilds: %s", ", ".join(MESSAGE_CACHE_GUILD_IDS))
        else:
            logging.info("Message caching enabled for all guilds")

    @bot_client.event
    async def on_message(message):
        # Skip bot messages and DMs
        if message.author.bot or not message.guild:
            return

        # Check if we should track messages in this guild
        if not should_track_messages(message.guild.id):
            if DEBUG_EVENTS:
                logging.debug("Skipping message from guild %s (not in allowlist)", message.guild.id)
            return

        # Get user ID
        user_id = uid(message.author.id)
        
        # Prepare message content
        content = message.content or "[Attachment/Embed]"
        
        # Add attachment information if present
        if message.attachments:
            attachment_urls = "\n".join(f"[Attachment] {attachment.url}" for attachment in message.attachments)
            content = f"{content}\n{attachment_urls}" if content and content != "[Attachment/Embed]" else attachment_urls
        
        # Skip empty messages
        if not content.strip():
            if DEBUG_EVENTS:
                logging.debug("Skipping empty content message from %s", message.author.name)
            return
        
        # Create message entry
        entry = {
            "content": content,
            "channel_id": str(message.channel.id),
            "timestamp": int(message.created_at.timestamp()),
            "channel_name": getattr(message.channel, "name", "unknown"),
            "timestamp_iso": message.created_at.isoformat(),
            "id": str(message.id),
            "author": message.author.display_name,
        }
        
        # Add to message buffer (automatically keeps only last 15 messages)
        _message_buffer[user_id].append(entry)
        
        # Update recent message context with timestamp
        _recent_message_context[user_id] = (list(_message_buffer[user_id]), time.time())
        
        if DEBUG_EVENTS:
            logging.debug("Cached message for %s (%s total)", message.author.name, len(_message_buffer[user_id]))
        
        # Periodically snapshot messages to database
        if len(_message_buffer[user_id]) % 5 == 0:  # Every 5 messages
            await maybe_snapshot_messages(user_id, message.guild.id)

    @bot_client.event
    async def on_member_ban(guild, user):
        user_id = uid(user.id)
        
        # Check if we should track messages in this guild
        if not should_track_messages(guild.id):
            return
        
        logging.info("Detected ban for user %s in guild %s", user_id, guild.id)
        
        # Get cached messages
        cached_msgs = list(_message_buffer.get(user_id, []))
        
        # If we have messages and Supabase is ready, save them
        if cached_msgs and is_supabase_ready():
            try:
                await supabase_request(
                    "post",
                    SUPABASE_CONTEXT_TABLE,
                    params={"on_conflict": "user_id"},
                    payload={
                        "user_id": user_id,
                        "messages": cached_msgs,
                        "banned_at": int(time.time()),
                        "guild_id": str(guild.id),
                    },
                    prefer="resolution=merge-duplicates",
                )
                logging.info("Saved %d messages to Supabase for %s", len(cached_msgs), user_id)
            except Exception as exc:
                logging.warning("Failed to store banned context for %s: %s", user_id, exc)
        
        # Clean up in-memory cache
        _message_buffer.pop(user_id, None)
        _recent_message_context.pop(user_id, None)

# --- Helper Functions with improved implementations ---
def normalize_language(lang: Optional[str]) -> str:
    """Normalize language code to supported format."""
    if not lang:
        return "en"
    
    # Extract primary language code
    lang = lang.split(",")[0].split(";")[0].strip().lower()
    if "-" in lang:
        lang = lang.split("-")[0]
    
    # Return if supported, otherwise default to English
    return lang if lang in LANG_STRINGS else "en"

def format_timestamp(value: Any) -> str:
    """Convert various timestamp formats to a human-readable string."""
    if not value:
        return ""
    
    try:
        if isinstance(value, str) and "T" in value:
            # ISO format
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            # Unix timestamp
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        
        # Format as readable date/time
        return dt.astimezone(timezone.utc).strftime("%b %d, %Y • %H:%M UTC")
    except Exception:
        return str(value)

def hash_value(raw: str) -> str:
    """Hash a value using SHA-256 with the secret key."""
    return hashlib.sha256(f"{SECRET_KEY}:{raw}".encode("utf-8", "ignore")).hexdigest()

def hash_ip(ip: str) -> str:
    """Hash an IP address for storage."""
    if not ip or ip == "unknown":
        return "unknown"
    return hash_value(ip)

async def detect_language(request: Request, lang_param: Optional[str] = None) -> str:
    """Detect the user's preferred language from request parameters or headers."""
    # Check explicit language parameter first
    if lang_param:
        return normalize_language(lang_param)
    
    # Check cookie
    cookie_lang = request.cookies.get("lang")
    if cookie_lang:
        return normalize_language(cookie_lang)
    
    # Check Accept-Language header
    accept = request.headers.get("accept-language", "")
    if accept:
        return normalize_language(accept.split(",")[0].strip())
    
    # Try to detect from IP (optional)
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
    
    # Default to English
    return "en"

async def get_strings(lang: str) -> Dict[str, str]:
    """Get language strings, with fallback to English if needed."""
    lang = normalize_language(lang)
    
    # Return if we have the language
    if lang in LANG_STRINGS:
        return LANG_STRINGS[lang]
    
    # Check cache
    if lang in LANG_CACHE:
        return LANG_CACHE[lang]
    
    # Try to translate from English
    base = LANG_STRINGS["en"]
    translated = {}
    
    # Only translate if translation service is available
    if LIBRETRANSLATE_URL:
        for key, text in base.items():
            translated[key] = await translate_text(text, target_lang=lang, source_lang="en")
    
    # Merge with base (fallback to English for missing translations)
    merged = {**base, **translated}
    LANG_CACHE[lang] = merged
    return merged

async def translate_text(text: str, target_lang: str = "en", source_lang: Optional[str] = None) -> str:
    """Translate text using LibreTranslate."""
    if not text or normalize_language(target_lang) == "en":
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
    
    # Return original text if translation fails
    return text

def clean_display_name(raw: str) -> str:
    """Clean a Discord display name."""
    if not raw:
        return ""
    
    # Remove discriminator if present
    if raw.endswith("#0"):
        return raw[:-2]
    
    return raw

def is_supabase_ready() -> bool:
    """Check if Supabase configuration is available."""
    return bool(SUPABASE_URL and SUPABASE_KEY)

def persist_user_session(response: Response, user_id: str, username: str, display_name: Optional[str] = None):
    """Persist a user session in a secure cookie."""
    token = serializer.dumps({
        "uid": user_id, 
        "uname": username, 
        "iat": time.time(), 
        "display_name": display_name or username
    })
    
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=PERSIST_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="Lax",
    )

def maybe_persist_session(response: Response, session: Optional[dict], refreshed: bool):
    """Persist a session if it was refreshed."""
    if session and refreshed:
        persist_user_session(
            response,
            session["uid"],
            session.get("uname") or "",
            display_name=session.get("display_name"),
        )

def read_user_session(request: Request) -> Optional[dict]:
    """Read and validate a user session from cookies."""
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    
    try:
        data = serializer.loads(raw)
        
        # Check if session is expired
        if time.time() - float(data.get("iat", 0)) > PERSIST_SESSION_SECONDS:
            return None
        
        return data
    except BadSignature:
        return None

async def refresh_session_profile(session: Optional[dict]) -> Tuple[Optional[dict], bool]:
    """Refresh a session profile with latest Discord user data."""
    if not session:
        return None, False
    
    user_id = session.get("uid")
    if not user_id:
        return session, False
    
    # Get valid access token
    token = await get_valid_access_token(str(user_id))
    if not token:
        return session, False
    
    try:
        # Fetch updated user data
        user = await fetch_discord_user(token)
        
        # Update session with new data
        uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
        display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
        
        updated = dict(session)
        updated["uname"] = uname_label
        updated["display_name"] = display_name
        updated["iat"] = time.time()
        
        return updated, True
    except Exception as exc:
        logging.debug("Profile refresh failed for %s: %s", user_id, exc)
        return session, False

async def supabase_request(method: str, table: str, *, params: Optional[dict] = None, payload: Optional[dict] = None, prefer: Optional[str] = None):
    """Make a request to Supabase with proper error handling."""
    if not is_supabase_ready():
        return None
    
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer or "return=representation",
    }
    
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    
    try:
        client = get_http_client()
        resp = await client.request(method, url, params=params, headers=headers, json=payload, timeout=10)
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
    appeal_reason_original: str,
    user_lang: str,
    message_cache: Optional[List[dict]],
    ip: str,
    forwarded_for: str,
    user_agent: str,
):
    """Log an appeal to Supabase with all relevant details."""
    payload = {
        "appeal_id": appeal_id,
        "user_id": user["id"],
        "username": user.get("username"),
        "guild_id": TARGET_GUILD_ID,
        "ban_reason": ban_reason,
        "ban_evidence": ban_evidence,
        "appeal_reason": appeal_reason,
        "appeal_reason_original": appeal_reason_original,
        "user_lang": user_lang,
        "status": "pending",
        "ip": ip,
        "forwarded_for": forwarded_for,
        "user_agent": user_agent,
        "message_cache": message_cache,
    }
    
    await supabase_request("post", SUPABASE_TABLE, payload=payload)

async def get_remote_last_submit(user_id: str) -> Optional[float]:
    """Get the last appeal submission timestamp for a user from Supabase."""
    recs = await supabase_request(
        "get",
        SUPABASE_SESSION_TABLE,
        params={"user_id": f"eq.{user_id}", "order": "last_submit.desc", "limit": 1},
    )
    
    if recs:
        try:
            return float(recs[0].get("last_submit") or 0)
        except Exception:
            return None
    
    return None

async def is_session_token_used(token_hash: str) -> bool:
    """Check if a session token has already been used."""
    recs = await supabase_request(
        "get",
        SUPABASE_SESSION_TABLE,
        params={"token_hash": f"eq.{token_hash}", "limit": 1},
    )
    
    return bool(recs)

async def mark_session_token(token_hash: str, user_id: str, ts: float):
    """Mark a session token as used."""
    payload = {"token_hash": token_hash, "user_id": user_id, "last_submit": int(ts)}
    await supabase_request(
        "post",
        SUPABASE_SESSION_TABLE,
        payload=payload,
        prefer="resolution=merge-duplicates",
    )

async def update_appeal_status(
    appeal_id: str,
    status: str,
    moderator_id: Optional[str],
    dm_delivered: bool,
    notes: Optional[str] = None,
):
    """Update the status of an appeal in Supabase."""
    payload = {
        "status": status,
        "decision_by": moderator_id,
        "decision_at": int(time.time()),
        "dm_delivered": dm_delivered,
        "notes": notes,
    }
    
    await supabase_request("patch", SUPABASE_TABLE, params={"appeal_id": f"eq.{appeal_id}"}, payload=payload)

async def fetch_appeal_history(user_id: str, limit: int = 25) -> List[dict]:
    """Fetch appeal history for a user from Supabase."""
    params = {"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": min(limit, 100)}
    records = await supabase_request("get", SUPABASE_TABLE, params=params)
    
    return records or []

async def fetch_appeal_record(appeal_id: str) -> Optional[dict]:
    """Fetch a specific appeal record from Supabase."""
    records = await supabase_request(
        "get",
        SUPABASE_TABLE,
        params={"appeal_id": f"eq.{appeal_id}", "limit": 1},
    )
    
    if records:
        return records[0]
    
    return None

def render_page(title: str, body_html: str, lang: str = "en", strings: Optional[Dict[str, str]] = None, user_session: Optional[dict] = None) -> str:
    """Render a complete HTML page with the provided content."""
    lang = normalize_language(lang)
    year = time.gmtime().tm_year
    strings = strings or LANG_STRINGS["en"]
    
    # Build user chip if session is available
    user_chip = build_user_chip(user_session) if user_session else ""
    
    # Language toggle
    toggle_lang = "es" if lang != "es" else "en"
    toggle_label = strings.get("language_switch", "Language")
    
    # CSP and security
    script_nonce = secrets.token_urlsafe(12)
    csp = (
        "default-src 'self'; "
        "img-src 'self' data: https://*.discordapp.com https://*.discord.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'nonce-" + script_nonce + "'; "
        "connect-src 'self' https://discord.com https://*.discord.com; "
    )
    
    # Favicon
    favicon = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%235865F2'/%3E%3Cpath d='M42 10 28 24l4 4-6 6 4 4-6 6-6-6 6-6-4-4 6-6 4 4 6-6 4 4 6-6-10-10Z' fill='white'/%3E%3C/svg%3E"
    
    # Build the complete HTML page
    return f"""
    <!DOCTYPE html>
    <html lang="{html.escape(lang)}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)} - {html.escape(strings.get("site_title", "BlockSpin Appeals Portal"))}</title>
        <link rel="icon" type="image/svg+xml" href="{favicon}">
        <meta http-equiv="Content-Security-Policy" content="{html.escape(csp)}">
        <style>{BASE_STYLES}</style>
      </head>
      <body>
        <div class="app">
          <div class="header">
            <div class="brand">
              <div class="logo">BS</div>
              <div class="brand-text">
                <h1>BlockSpin</h1>
                <span>Appeals Portal</span>
              </div>
            </div>
            {user_chip}
          </div>

          {body_html}

          <div class="footer">
            <div>{html.escape(strings.get("footer_copyright", "").format(year=year))}</div>
            <div style="margin-top:8px;">
              <a href="/privacy" style="margin-right: 15px;">{html.escape(strings.get("footer_privacy", "Privacy Policy"))}</a>
              <a href="/terms">{html.escape(strings.get("footer_terms", "Terms of Service"))}</a>
              <span style="margin: 0 10px;">•</span>
              <a href="?lang={html.escape(toggle_lang)}">{html.escape(toggle_label)}</a>
            </div>
          </div>
        </div>
        
        <script nonce="{script_nonce}">
          // Add any client-side functionality here
          document.addEventListener('DOMContentLoaded', function() {{
            // Add fade-in animation to cards
            const cards = document.querySelectorAll('.card');
            cards.forEach((card, index) => {{
              setTimeout(() => {{
                card.classList.add('fade-in');
              }}, index * 100);
            }});
          }});
        </script>
      </body>
    </html>
    """

def wants_html(request: Request) -> bool:
    """Check if the client wants an HTML response."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept

def render_error(title: str, message: str, status_code: int = 400, lang: str = "en", strings: Optional[Dict[str, str]] = None, retry_url: Optional[str] = None) -> HTMLResponse:
    """Render an error page with the provided message."""
    safe_title = html.escape(title)
    safe_msg = html.escape(message)
    strings = strings or LANG_STRINGS["en"]
    
    content = ERROR_TEMPLATE.format(
        message=safe_msg,
        retry_url=retry_url or "",
        **strings
    )
    
    return HTMLResponse(
        render_page(title, content, lang=lang, strings=strings),
        status_code=status_code,
        headers={"Cache-Control": "no-store"}
    )

def build_user_chip(session: Optional[dict]) -> str:
    """Build a user chip HTML element from session data."""
    if not session:
        return ""
    
    name = clean_display_name(session.get("display_name") or session.get("uname") or "")
    return f"""
      <div class="user-chip">
        <span class="name">{html.escape(name)}</span>
        <div class="actions"><a href="/logout">{html.escape(LANG_STRINGS[normalize_language(session.get('lang', 'en'))].get('logout', 'Logout'))}</a></div>
      </div>
    """

# --- Exception Handlers with improved error pages ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if wants_html(request):
        msg = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
        lang = await detect_language(request)
        strings = await get_strings(lang)
        return render_error("Request failed", msg, exc.status_code, lang=lang, strings=strings)
    
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if wants_html(request):
        lang = await detect_language(request)
        strings = await get_strings(lang)
        return render_error("Invalid input", "Please check the form and try again.", 422, lang=lang, strings=strings)
    
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.exception("Unhandled error: %s", exc)
    
    if wants_html(request):
        lang = await detect_language(request)
        strings = await get_strings(lang)
        return render_error("Server error", "An unexpected error occurred. Please try again later.", 500, lang=lang, strings=strings)
    
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# --- OAuth and Authentication Functions ---
def oauth_authorize_url(state: str) -> str:
    """Generate the OAuth authorization URL for Discord."""
    return (
        f"{DISCORD_API_BASE}/oauth2/authorize"
        f"?response_type=code&client_id={DISCORD_CLIENT_ID}"
        f"&scope={OAUTH_SCOPES}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&state={state}"
        f"&prompt=none"
    )

def get_client_ip(request: Request) -> str:
    """Get the client IP address from the request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    
    if request.client:
        return request.client.host or "unknown"
    
    return "unknown"

def issue_state_token(ip: str) -> str:
    """Issue a state token for OAuth flow."""
    token = secrets.token_urlsafe(16)
    now = time.time()
    _state_tokens[token] = (ip, now)
    
    # Prune stale tokens (>15 minutes)
    for t, (_, ts) in list(_state_tokens.items()):
        if now - ts > 900:
            _state_tokens.pop(t, None)
    
    return token

def validate_state_token(token: str, ip: str) -> bool:
    """Validate a state token from OAuth flow."""
    if not token:
        return False
    
    record = _state_tokens.pop(token, None)
    if not record:
        return False
    
    saved_ip, ts = record
    if time.time() - ts > 900:
        return False
    
    if ip in {"unknown", "", None} or saved_ip in {"unknown", "", None}:
        return False
    
    if saved_ip != ip:
        return False
    
    return True

def enforce_ip_rate_limit(ip: str):
    """Enforce IP-based rate limiting."""
    now = time.time()
    window_start = now - APPEAL_IP_WINDOW_SECONDS
    
    # Clean up old entries if the dictionary gets too large
    if len(_ip_requests) > 10000:
        _ip_requests.clear()
    
    # Get or create the IP bucket
    bucket = _ip_requests.setdefault(ip, [])
    
    # Filter out old requests
    bucket = [t for t in bucket if t >= window_start]
    
    # Check if rate limit exceeded
    if len(bucket) >= APPEAL_IP_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    
    # Add current request
    bucket.append(now)
    _ip_requests[ip] = bucket

async def exchange_code_for_token(code: str) -> dict:
    """Exchange an OAuth code for an access token."""
    try:
        client = get_http_client()
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

def store_user_token(user_id: str, token_data: dict):
    """Store user token data in memory."""
    expires_in = float(token_data.get("expires_in") or 0)
    _user_tokens[user_id] = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": time.time() + expires_in - 60 if expires_in else None,
        "token_type": token_data.get("token_type", "Bearer"),
    }

async def refresh_user_token(user_id: str) -> Optional[str]:
    """Refresh a user's access token."""
    token_data = _user_tokens.get(user_id) or {}
    refresh_token = token_data.get("refresh_token")
    
    if not refresh_token:
        return None
    
    try:
        client = get_http_client()
        resp = await client.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        new_token = resp.json()
        store_user_token(user_id, new_token)
        return new_token.get("access_token")
    except Exception as exc:
        logging.warning("Failed to refresh token for user %s: %s", user_id, exc)
        return None

async def get_valid_access_token(user_id: str) -> Optional[str]:
    """Get a valid access token for a user, refreshing if necessary."""
    token_data = _user_tokens.get(user_id) or {}
    access_token = token_data.get("access_token")
    expires_at = token_data.get("expires_at")
    
    if not access_token:
        return None
    
    # Refresh if expired
    if expires_at and time.time() > expires_at:
        return await refresh_user_token(user_id)
    
    return access_token

async def fetch_discord_user(access_token: str) -> dict:
    """Fetch user information from Discord using an access token."""
    client = get_http_client()
    resp = await client.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()

async def fetch_ban_if_exists(user_id: str) -> Optional[dict]:
    """Check if a user is banned in the target guild."""
    client = get_http_client()
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
    
    token = await get_valid_access_token(user_id)
    if not token:
        return False
    
    client = get_http_client()
    resp = await client.put(
        f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"access_token": token},
    )
    
    added = resp.status_code in (200, 201, 204)
    
    if added and CLEANUP_DM_INVITES:
        try:
            # Clean up invites to prevent abuse
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
        except Exception as exc:  # Best-effort cleanup
            logging.exception("Failed invite cleanup: %s", exc)
    
    return added

async def maybe_remove_from_dm_guild(user_id: str):
    """Remove a user from the DM guild if configured."""
    if not DM_GUILD_ID or not REMOVE_FROM_DM_GUILD_AFTER_DM:
        return
    
    client = get_http_client()
    await client.delete(
        f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )

async def remove_from_target_guild(user_id: str) -> Optional[int]:
    """Remove a user from the target guild."""
    client = get_http_client()
    resp = await client.delete(
        f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )
    
    if resp.status_code not in (200, 204, 404):
        logging.warning("Failed to remove user %s from guild %s: %s %s", user_id, TARGET_GUILD_ID, resp.status_code, resp.text)
    
    return resp.status_code

async def add_user_to_guild(user_id: str, guild_id: str) -> Optional[int]:
    """Add a user to a guild using their OAuth token."""
    token = await get_valid_access_token(user_id)
    if not token:
        logging.warning("No OAuth token cached for user %s; cannot re-add to guild %s", user_id, guild_id)
        return None
    
    client = get_http_client()
    resp = await client.put(
        f"{DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"access_token": token},
    )
    
    if resp.status_code not in (200, 201, 204):
        logging.warning("Failed to add user %s to guild %s: %s %s", user_id, guild_id, resp.status_code, resp.text)
    
    return resp.status_code

async def send_log_message(content: str):
    """Send a log message to the auth/ops channel."""
    try:
        client = get_http_client()
        resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{AUTH_LOG_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"content": content},
            timeout=10,
        )
        
        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(min(retry, 5.0))
            return await client.post(
                f"{DISCORD_API_BASE}/channels/{AUTH_LOG_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                json={"content": content},
            )
        
        resp.raise_for_status()
    except Exception as exc:
        logging.warning("Log post failed: %s", exc)

async def maybe_snapshot_messages(user_id: str, guild_id: str):
    """Snapshot messages for a user to persistent storage."""
    if not is_supabase_ready():
        return
    
    if not should_track_messages(guild_id):
        logging.debug("Message caching skipped for guild %s", guild_id)
        return
    
    entries = list(_message_buffer.get(user_id, []))
    if not entries:
        return
    
    await persist_message_snapshot(user_id, entries[-15:])

async def persist_message_snapshot(user_id: str, messages: List[dict]):
    """Persist a message snapshot to Supabase."""
    if not is_supabase_ready() or not messages:
        return
    
    logging.info("Persisting %d messages for user %s", len(messages[-15:]), user_id)
    
    try:
        await supabase_request(
            "post",
            "user_message_snapshots",
            params={"on_conflict": "user_id"},
            payload={"user_id": user_id, "messages": messages[-15:]},
            prefer="resolution=merge-duplicates",
        )
    except Exception as exc:
        logging.warning("Snapshot persist failed for %s: %s", user_id, exc)

async def fetch_message_cache(user_id: str, limit: int = 15) -> List[dict]:
    """Fetch message cache for a user from Supabase or in-memory cache."""
    if not is_supabase_ready():
        return _get_recent_message_context(user_id, limit)
    
    try:
        # Try to get from Supabase first
        recs = await supabase_request(
            "get",
            SUPABASE_CONTEXT_TABLE,
            params={"user_id": f"eq.{user_id}", "limit": 1},
        )
        
        if recs and recs[0].get("messages"):
            messages = recs[0]["messages"]
            
            # Sort by timestamp
            def get_ts(m: dict) -> float:
                t = m.get("timestamp", 0)
                try:
                    return float(t)
                except Exception:
                    return 0.0
            
            return sorted(messages, key=get_ts, reverse=True)[:limit]
    except Exception as exc:
        logging.warning("Failed to fetch context for %s: %s", user_id, exc)
    
    # Fall back to in-memory cache
    return _get_recent_message_context(user_id, limit)

def _get_recent_message_context(user_id: str, limit: int) -> List[dict]:
    """Get recent message context from in-memory cache."""
    entry = _recent_message_context.get(user_id)
    if not entry:
        return []
    
    messages, ts = entry
    if time.time() - ts > RECENT_MESSAGE_CACHE_TTL:
        _recent_message_context.pop(user_id, None)
        return []
    
    def _timestamp_value(msg: dict) -> float:
        try:
            return float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0.0
    
    return sorted(messages, key=_timestamp_value, reverse=True)[:limit]

# --- Routes with improved UI/UX ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render the home page."""
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Check if user is already logged in
    session = read_user_session(request)
    if session:
        # Refresh session profile if needed
        session, refreshed = await refresh_session_profile(session)
        
        # Create response
        response = RedirectResponse(url="/status", status_code=302)
        
        # Persist session if refreshed
        maybe_persist_session(response, session, refreshed)
        
        return response
    
    # Render home page
    body_html = HOME_TEMPLATE.format(**strings)
    return HTMLResponse(render_page(strings["site_title"], body_html, lang=lang, strings=strings))

@app.get("/login")
async def login(request: Request):
    """Redirect to Discord OAuth login."""
    ip = get_client_ip(request)
    state = issue_state_token(ip)
    
    # Store language preference in state for after redirect
    lang = await detect_language(request)
    state_with_lang = f"{state}:{lang}"
    
    auth_url = oauth_authorize_url(state_with_lang)
    return RedirectResponse(url=auth_url)

@app.get("/callback")
async def callback(request: Request, code: str, state: str):
    """Handle OAuth callback from Discord."""
    ip = get_client_ip(request)
    
    # Extract language from state if present
    lang = "en"
    if ":" in state:
        state, lang_param = state.split(":", 1)
        lang = normalize_language(lang_param)
    
    strings = await get_strings(lang)
    
    # Validate state token
    if not validate_state_token(state, ip):
        return render_error(
            "Authentication failed",
            "Invalid authentication state. Please try logging in again.",
            400,
            lang=lang,
            strings=strings,
            retry_url="/login"
        )
    
    # Exchange code for token
    try:
        token_data = await exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        
        # Fetch user information
        user = await fetch_discord_user(access_token)
        user_id = uid(user["id"])
        
        # Store token for later use
        store_user_token(user_id, token_data)
        
        # Check if user is banned
        ban_info = await fetch_ban_if_exists(user_id)
        if not ban_info:
            return render_error(
                "No active ban",
                "Your account does not have an active ban in the server.",
                400,
                lang=lang,
                strings=strings,
                retry_url="/"
            )
        
        # Create session
        uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
        display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
        
        session = {
            "uid": user_id,
            "uname": uname_label,
            "display_name": display_name,
            "iat": time.time(),
            "lang": lang,
        }
        
        # Create response with session cookie
        response = RedirectResponse(url="/appeal", status_code=302)
        persist_user_session(response, user_id, uname_label, display_name)
        
        # Log authentication
        await send_log_message(f"User {user_id} ({uname_label}) authenticated for appeal process")
        
        return response
        
    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("Authentication error: %s", exc)
        return render_error(
            "Authentication failed",
            "An error occurred during authentication. Please try again.",
            500,
            lang=lang,
            strings=strings,
            retry_url="/login"
        )

@app.get("/appeal", response_class=HTMLResponse)
async def appeal_page(request: Request):
    """Render the appeal submission page."""
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Check user session
    session = read_user_session(request)
    if not session:
        return RedirectResponse(url="/login")
    
    # Refresh session profile if needed
    session, refreshed = await refresh_session_profile(session)
    
    # Create response
    response = HTMLResponse()
    
    # Persist session if refreshed
    maybe_persist_session(response, session, refreshed)
    
    # Get user ID
    user_id = session["uid"]
    
    # Check if user is banned
    ban_info = await fetch_ban_if_exists(user_id)
    if not ban_info:
        return render_error(
            "No active ban",
            "Your account does not have an active ban in the server.",
            400,
            lang=lang,
            strings=strings,
            retry_url="/status"
        )
    
    # Get message context
    messages = await fetch_message_cache(user_id)
    
    # Format messages for display
    formatted_messages = []
    for msg in messages:
        formatted_messages.append({
            "content": msg.get("content", ""),
            "channel_name": msg.get("channel_name", "unknown"),
            "timestamp": format_timestamp(msg.get("timestamp")),
            "author": session.get("display_name", "You"),
        })
    
    # Render appeal page
    body_html = APPEAL_TEMPLATE.format(
        ban_reason=ban_info.get("reason", "No reason provided"),
        ban_date=format_timestamp(ban_info.get("audit_log_entry", {}).get("creation_timestamp")),
        messages=formatted_messages,
        **strings
    )
    
    return HTMLResponse(
        render_page(strings["site_title"], body_html, lang=lang, strings=strings, user_session=session),
        headers=response.headers
    )

@app.post("/submit-appeal")
async def submit_appeal(
    request: Request,
    appeal_reason: str = Form(...),
    additional_info: str = Form("")
):
    """Process an appeal submission."""
    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    user_agent = request.headers.get("User-Agent", "")
    
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Check user session
    session = read_user_session(request)
    if not session:
        return render_error(
            "Authentication required",
            "You must be logged in to submit an appeal.",
            401,
            lang=lang,
            strings=strings,
            retry_url="/login"
        )
    
    # Refresh session profile if needed
    session, refreshed = await refresh_session_profile(session)
    
    # Get user ID
    user_id = session["uid"]
    
    # Check rate limiting
    now = time.time()
    last_submit = _appeal_rate_limit.get(user_id, 0)
    if now - last_submit < APPEAL_COOLDOWN_SECONDS:
        remaining = int(APPEAL_COOLDOWN_SECONDS - (now - last_submit))
        return render_error(
            "Rate limit exceeded",
            f"You must wait {remaining} seconds before submitting another appeal.",
            429,
            lang=lang,
            strings=strings,
            retry_url="/appeal"
        )
    
    # Check if user has already appealed
    if _appeal_locked.get(user_id, False):
        return render_error(
            "Appeal already submitted",
            "You have already submitted an appeal. Please wait for a decision.",
            400,
            lang=lang,
            strings=strings,
            retry_url="/status"
        )
    
    # Enforce IP rate limiting
    enforce_ip_rate_limit(ip)
    
    # Check if user is banned
    ban_info = await fetch_ban_if_exists(user_id)
    if not ban_info:
        return render_error(
            "No active ban",
            "Your account does not have an active ban in the server.",
            400,
            lang=lang,
            strings=strings,
            retry_url="/status"
        )
    
    # Generate appeal ID
    appeal_id = str(uuid.uuid4())
    
    # Get message context
    messages = await fetch_message_cache(user_id)
    
    # Log appeal to Supabase
    await log_appeal_to_supabase(
        appeal_id=appeal_id,
        user={"id": user_id, "username": session.get("uname")},
        ban_reason=ban_info.get("reason", "No reason provided"),
        ban_evidence="",  # Could be populated with additional evidence
        appeal_reason=appeal_reason,
        appeal_reason_original=appeal_reason,  # Store original language
        user_lang=lang,
        message_cache=messages,
        ip=hash_ip(ip),
        forwarded_for=hash_ip(forwarded_for) if forwarded_for else "",
        user_agent=user_agent,
    )
    
    # Update rate limiting
    _appeal_rate_limit[user_id] = now
    _appeal_locked[user_id] = True
    
    # Send notification to Discord
    try:
        client = get_http_client()
        
        # Create embed for appeal notification
        embed = {
            "title": "New Ban Appeal",
            "description": f"User: {session.get('uname')}\nUser ID: {user_id}\nAppeal ID: {appeal_id}",
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "Ban Reason",
                    "value": ban_info.get("reason", "No reason provided"),
                    "inline": False
                },
                {
                    "name": "Appeal Reason",
                    "value": appeal_reason,
                    "inline": False
                }
            ],
            "footer": {
                "text": f"Submitted at {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}"
            }
        }
        
        if additional_info:
            embed["fields"].append({
                "name": "Additional Information",
                "value": additional_info,
                "inline": False
            })
        
        # Send to appeal channel
        await client.post(
            f"{DISCORD_API_BASE}/channels/{APPEAL_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed]}
        )
        
        # Log to auth log channel
        await send_log_message(f"Appeal submitted by {user_id} ({session.get('uname')}) - ID: {appeal_id}")
        
    except Exception as exc:
        logging.warning("Failed to send appeal notification: %s", exc)
    
    # Create response with session cookie
    response = RedirectResponse(url="/status?submitted=true", status_code=302)
    
    # Persist session if refreshed
    maybe_persist_session(response, session, refreshed)
    
    return response

@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Render the status page for appeal history."""
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Check user session
    session = read_user_session(request)
    if not session:
        return RedirectResponse(url="/login")
    
    # Refresh session profile if needed
    session, refreshed = await refresh_session_profile(session)
    
    # Create response
    response = HTMLResponse()
    
    # Persist session if refreshed
    maybe_persist_session(response, session, refreshed)
    
    # Get user ID
    user_id = session["uid"]
    
    # Check if user is banned
    ban_info = await fetch_ban_if_exists(user_id)
    can_appeal = ban_info is not None and not _appeal_locked.get(user_id, False)
    
    # Fetch appeal history
    appeals = await fetch_appeal_history(user_id)
    
    # Format appeals for display
    formatted_appeals = []
    for appeal in appeals:
        formatted_appeals.append({
            "appeal_id": appeal.get("appeal_id", ""),
            "status": appeal.get("status", "pending"),
            "created_at": format_timestamp(appeal.get("created_at")),
            "ban_reason": appeal.get("ban_reason", ""),
            "appeal_reason": appeal.get("appeal_reason", ""),
        })
    
    # Check if just submitted an appeal
    submitted = request.query_params.get("submitted", "false").lower() == "true"
    
    # Render status page
    body_html = STATUS_TEMPLATE.format(
        appeals=formatted_appeals,
        can_appeal=can_appeal,
        **strings
    )
    
    # Add success message if just submitted
    if submitted:
        success_alert = f'<div class="alert alert-success">Your appeal has been submitted successfully. You will be notified when a decision is made.</div>'
        body_html = success_alert + body_html
    
    return HTMLResponse(
        render_page(strings["site_title"], body_html, lang=lang, strings=strings, user_session=session),
        headers=response.headers
    )

@app.get("/logout")
async def logout(request: Request):
    """Log out the current user."""
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Create response with cleared session cookie
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    
    return response

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    """Render the privacy policy page."""
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Check user session
    session = read_user_session(request)
    
    # Simple privacy policy content
    privacy_content = """
    <div class="card">
        <h2>Privacy Policy</h2>
        <p>This privacy policy explains how BlockSpin ("we," "us," or "our") collects, uses, and protects your information when you use our Discord appeals portal.</p>
        
        <h3>Information We Collect</h3>
        <p>When you use our appeals portal, we collect the following information:</p>
        <ul>
            <li>Your Discord user ID and username</li>
            <li>Information about your ban from the Discord server</li>
            <li>Your appeal submission and any additional information you provide</li>
            <li>Recent messages you sent in the server (for context)</li>
            <li>Technical information such as your IP address and browser details</li>
        </ul>
        
        <h3>How We Use Your Information</h3>
        <p>We use the information we collect to:</p>
        <ul>
            <li>Process and review your ban appeal</li>
            <li>Communicate with you about your appeal status</li>
            <li>Improve our services and prevent abuse</li>
        </ul>
        
        <h3>Data Retention</h3>
        <p>Your appeal information is retained for as long as necessary to process your appeal and for a reasonable period thereafter for record-keeping purposes.</p>
        
        <h3>Your Rights</h3>
        <p>You have the right to:</p>
        <ul>
            <li>Access the information we hold about you</li>
            <li>Request correction of inaccurate information</li>
            <li>Request deletion of your information (subject to legal obligations)</li>
        </ul>
        
        <h3>Changes to This Policy</h3>
        <p>We may update this privacy policy from time to time. We will notify you of any changes by posting the new policy on this page.</p>
        
        <h3>Contact Us</h3>
        <p>If you have any questions about this privacy policy, please contact us through our Discord server.</p>
    </div>
    """
    
    return HTMLResponse(
        render_page("Privacy Policy", privacy_content, lang=lang, strings=strings, user_session=session)
    )

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    """Render the terms of service page."""
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Check user session
    session = read_user_session(request)
    
    # Simple terms of service content
    terms_content = """
    <div class="card">
        <h2>Terms of Service</h2>
        <p>By using the BlockSpin Discord appeals portal ("Service"), you agree to comply with and be bound by the following terms and conditions.</p>
        
        <h3>Acceptance of Terms</h3>
        <p>By accessing or using our Service, you agree to be bound by these Terms. If you disagree with any part of the terms, you may not access the Service.</p>
        
        <h3>Use of the Service</h3>
        <p>You agree to use our Service only for lawful purposes and in accordance with these Terms. You agree not to:</p>
        <ul>
            <li>Submit false or misleading information in your appeal</li>
            <li>Use the Service to harass, abuse, or harm others</li>
            <li>Attempt to gain unauthorized access to our systems</li>
            <li>Interfere with or disrupt the Service</li>
        </ul>
        
        <h3>Appeal Process</h3>
        <p>When submitting an appeal, you agree to provide accurate information to the best of your knowledge. The BlockSpin moderation team reserves the right to accept or reject any appeal at their discretion.</p>
        
        <h3>Intellectual Property</h3>
        <p>The Service and its original content, features, and functionality are owned by BlockSpin and are protected by international copyright, trademark, and other intellectual property laws.</p>
        
        <h3>Termination</h3>
        <p>We may terminate or suspend your access to our Service immediately, without prior notice, for any reason, including if you breach the Terms.</p>
        
        <h3>Changes to Terms</h3>
        <p>We reserve the right to modify or replace these Terms at any time. If a revision is material, we will provide at least 30 days notice prior to any new terms taking effect.</p>
        
        <h3>Contact Information</h3>
        <p>If you have any questions about these Terms, please contact us through our Discord server.</p>
    </div>
    """
    
    return HTMLResponse(
        render_page("Terms of Service", terms_content, lang=lang, strings=strings, user_session=session)
    )

# --- Discord Interaction Endpoints ---
@app.post("/interactions")
async def interactions(request: Request):
    """Handle Discord interactions (slash commands, components, etc.)."""
    if not DISCORD_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="Discord public key not configured")
    
    # Get request body
    body = await request.json()
    
    # Verify signature
    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    
    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="Missing signature headers")
    
    # This is a simplified verification - in a real implementation, you would verify the signature
    # using the DISCORD_PUBLIC_KEY and the request body
    
    # Handle different interaction types
    interaction_type = body.get("type")
    
    if interaction_type == 1:  # PING
        return {"type": 1}  # PONG response
    
    elif interaction_type == 2:  # APPLICATION_COMMAND
        # Handle slash commands
        command_name = body.get("data", {}).get("name")
        
        if command_name == "appeal":
            # Handle appeal command
            user_id = body.get("member", {}).get("user", {}).get("id")
            
            if not user_id:
                return {
                    "type": 4,
                    "data": {
                        "content": "Could not retrieve user information.",
                        "flags": 64  # EPHEMERAL
                    }
                }
            
            # Check if user is banned
            ban_info = await fetch_ban_if_exists(user_id)
            if not ban_info:
                return {
                    "type": 4,
                    "data": {
                        "content": "You do not have an active ban in this server.",
                        "flags": 64  # EPHEMERAL
                    }
                }
            
            # Check if user has already appealed
            if _appeal_locked.get(user_id, False):
                return {
                    "type": 4,
                    "data": {
                        "content": "You have already submitted an appeal. Please wait for a decision.",
                        "flags": 64  # EPHEMERAL
                    }
                }
            
            # Generate appeal link
            appeal_link = f"https://bs-appeals.up.railway.app/login"
            
            return {
                "type": 4,
                "data": {
                    "content": f"You can submit an appeal at: {appeal_link}",
                    "flags": 64  # EPHEMERAL
                }
            }
    
    # Default response
    return {"type": 1}

# --- Health Check Endpoint ---
@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "timestamp": time.time()}

# --- Run the Application ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)