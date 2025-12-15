import os
import secrets
import uuid
import asyncio
import time
import html
import copy
import hashlib
from datetime import datetime, timezone
from collections import deque, defaultdict
from typing import Optional, Tuple, Dict, List, Any

try:
    import discord  # type: ignore
except ImportError:  # allow app to boot even if discord.py isn't installed
    discord = None

import httpx
from jinja2 import Environment, select_autoescape
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
AUTH_LOG_CHANNEL_ID = int(os.getenv("AUTH_LOG_CHANNEL_ID", "1449822248490762421"))
SECRET_KEY = os.getenv("PORTAL_SECRET_KEY") or secrets.token_hex(16)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "discord-appeals"
SUPABASE_SESSION_TABLE = "discord-appeal-sessions"
SUPABASE_CONTEXT_TABLE = "banned_user_context"
INVITE_LINK = "https://discord.gg/blockspin"
MESSAGE_CACHE_GUILD_IDS_RAW = os.getenv("MESSAGE_CACHE_GUILD_ID", "").strip()
READD_GUILD_ID = os.getenv("READD_GUILD_ID", "1065973360040890418")
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate")
DEBUG_EVENTS = os.getenv("DEBUG_EVENTS", "false").lower() == "true"
BOT_EVENT_LOGGING = os.getenv("BOT_EVENT_LOGGING", "").lower() in {"1", "true", "yes"} or DEBUG_EVENTS
BOT_MESSAGE_LOG_CONTENT = os.getenv("BOT_MESSAGE_LOG_CONTENT", "").lower() in {"1", "true", "yes"} or DEBUG_EVENTS

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
    global _bot_task
    if _bot_task and not _bot_task.done():
        return

    async def _run_bot():
        try:
            logging.info("Starting Discord bot gateway connection...")
            await bot_client.start(DISCORD_BOT_TOKEN)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logging.exception("Discord bot task crashed: %s", exc)

    _bot_task = asyncio.create_task(_run_bot())


@app.on_event("shutdown")
async def shutdown_event():
    global _bot_task
    if bot_client:
        try:
            await bot_client.close()
        except Exception:
            pass
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()

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

# simple in-memory stores
_appeal_rate_limit: Dict[str, float] = {}  # {user_id: timestamp_of_last_submit}
_used_sessions: Dict[str, float] = {}  # {session_token: timestamp_used}
_ip_requests: Dict[str, List[float]] = {}  # {ip: [timestamps]}
_ban_first_seen: Dict[str, float] = {}  # {user_id: first time we saw the ban}
_appeal_locked: Dict[str, bool] = {}  # {user_id: True if appealed already}
_user_tokens: Dict[str, Dict[str, Any]] = {}  # {user_id: {"access_token": str, "refresh_token": str, "expires_at": float}}
_processed_appeals: Dict[str, float] = {}  # {appeal_id: timestamp_processed}
_declined_users: Dict[str, bool] = {}  # {user_id: True if appeal declined}
_state_tokens: Dict[str, Tuple[str, float]] = {}  # {token: (ip, issued_at)}
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

# --- Bot & Cache Setup ---
bot_client = None
_bot_task: Optional[asyncio.Task] = None
_message_buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=15))
_recent_message_context: Dict[str, Tuple[List[dict], float]] = {}
RECENT_MESSAGE_CACHE_TTL = int(os.getenv("RECENT_MESSAGE_CACHE_TTL", "3600"))

_raw_cache_guilds = [
    gid.strip()
    for gid in MESSAGE_CACHE_GUILD_IDS_RAW.split(",")
    if gid.strip()
]
# If no allowlist is provided, track all guilds (safer default for not missing context).
MESSAGE_CACHE_GUILD_IDS = set(_raw_cache_guilds) if _raw_cache_guilds else None
if MESSAGE_CACHE_GUILD_IDS is not None and TARGET_GUILD_ID and TARGET_GUILD_ID != "0":
    MESSAGE_CACHE_GUILD_IDS.add(TARGET_GUILD_ID)


def uid(value: Any) -> str:
    return str(value)

def _truncate_log_text(value: str, limit: int = 260) -> str:
    value = (value or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return value[:limit] + "…"

def should_track_messages(guild_id: int) -> bool:
    if MESSAGE_CACHE_GUILD_IDS is None:
        return True
    return str(guild_id) in MESSAGE_CACHE_GUILD_IDS

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

    @bot_client.event
    async def on_message(message):
        if message.author.bot or not message.guild:
            return

        if not should_track_messages(message.guild.id):
            logging.debug("Skipping message cache for guild %s", message.guild.id)
            if DEBUG_EVENTS:
                print(f"[DEBUG] Skipping message from guild {message.guild.id} (Not in allowlist)")
            return

        user_id = uid(message.author.id)
        content = message.content or "[Attachment/Embed]"
        if message.attachments:
            attachment_urls = "\n".join(f"[Attachment] {attachment.url}" for attachment in message.attachments)
            content = f"{content}\n{attachment_urls}" if content and content != "[Attachment/Embed]" else content
        if not content.strip():
            if DEBUG_EVENTS:
                print(f"[DEBUG] Dropping empty content message from {message.author.name}")
            return
        if BOT_EVENT_LOGGING:
            user_label = getattr(message.author, "global_name", None) or getattr(message.author, "display_name", None) or getattr(message.author, "name", "unknown")
            channel_name = getattr(message.channel, "name", "unknown")
            log_content = _truncate_log_text(content) if BOT_MESSAGE_LOG_CONTENT else f"<len={len(content)}>"
            logging.info(
                "[msg_cache] guild=%s channel=%s(#%s) user=%s(%s) msg=%s content=%s",
                message.guild.id,
                message.channel.id,
                channel_name,
                user_id,
                user_label,
                getattr(message, "id", "unknown"),
                log_content,
            )
        ts_str = message.created_at.isoformat()
        entry = {
            "content": content,
            "channel_id": str(message.channel.id),
            "timestamp": int(message.created_at.timestamp()),
            "channel_name": getattr(message.channel, "name", "unknown"),
            "timestamp_iso": ts_str,
            "id": str(message.id),
        }
        _message_buffer[user_id].append(entry)
        _recent_message_context[user_id] = (list(_message_buffer[user_id]), time.time())
        if DEBUG_EVENTS:
            print(f"[DEBUG] RAM Cache for {message.author.name}: {len(_message_buffer[user_id])} messages stored.")
        await maybe_snapshot_messages(user_id, message.guild.id)

    @bot_client.event
    async def on_member_ban(guild, user):
        user_id = uid(user.id)
        if not should_track_messages(guild.id):
            return

        logging.info("Detected ban for user %s in guild %s", user_id, guild.id)
        cached_msgs = list(_message_buffer.get(user_id, []))
        if not cached_msgs:
            cached_msgs = _get_recent_message_context(user_id, 15)
            if BOT_EVENT_LOGGING:
                logging.info("[ban_cache] user=%s guild=%s source=recent_context msgs=%s", user_id, guild.id, len(cached_msgs))
        elif BOT_EVENT_LOGGING:
            logging.info("[ban_cache] user=%s guild=%s source=ram_buffer msgs=%s", user_id, guild.id, len(cached_msgs))

        if is_supabase_ready():
            logging.info(
                "Upserting banned context user=%s msgs=%s table=%s",
                user_id,
                len(cached_msgs),
                SUPABASE_CONTEXT_TABLE,
            )
            result = await supabase_request(
                "post",
                SUPABASE_CONTEXT_TABLE,
                params={"on_conflict": "user_id"},
                payload={
                    "user_id": user_id,
                    "messages": cached_msgs,
                    "banned_at": int(time.time()),
                },
                prefer="resolution=merge-duplicates,return=representation",
            )
            if BOT_EVENT_LOGGING:
                logging.info(
                    "[ban_supabase] user=%s guild=%s ok=%s returned_rows=%s",
                    user_id,
                    guild.id,
                    bool(result is not None),
                    (len(result) if isinstance(result, list) else (1 if isinstance(result, dict) else 0)),
                )

        _message_buffer.pop(user_id, None)
        _recent_message_context.pop(user_id, None)

BASE_STYLES = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root{
  --bg:#07080c;
  --bg2:#0b0d14;
  --card:rgba(18,21,30,.72);
  --card2:rgba(14,16,24,.72);
  --border:rgba(255,255,255,.08);
  --border2:rgba(255,255,255,.12);

  --text:#edf2f7;
  --muted:#9aa4b2;
  --muted2:#7c8592;

  --accent:#5865F2;
  --accent2:#7c5cff;
  --accentGlow:rgba(88,101,242,.35);

  --danger:#ef4444;
  --dangerGlow:rgba(239,68,68,.25);
  --success:#22c55e;

  --radius:16px;
  --radius2:12px;

  --shadow: 0 18px 60px rgba(0,0,0,.45);
  --shadow2: 0 10px 32px rgba(0,0,0,.35);
}

*{ box-sizing:border-box; }
html,body{ height:100%; }

body{
  margin:0;
  font-family:"Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:
    radial-gradient(900px 600px at 15% 20%, rgba(88,101,242,.18), transparent 55%),
    radial-gradient(900px 600px at 85% 80%, rgba(239,68,68,.10), transparent 55%),
    linear-gradient(180deg, var(--bg), var(--bg2));
  color:var(--text);
  display:grid;
  place-items:center;
  padding:24px;
}

a{ color:inherit; }

.app{
  width:100%;
  max-width:980px;
  display:flex;
  flex-direction:column;
  gap:18px;
  animation:enter .5s cubic-bezier(.16,1,.3,1);
}

@keyframes enter{
  from{ opacity:0; transform:translateY(14px); }
  to{ opacity:1; transform:translateY(0); }
}

/* --- Top bar --- */
.brand-row{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
  padding:0 2px;
}

.brand{
  display:flex;
  align-items:center;
  gap:14px;
}

.logo{
  width:46px;height:46px;
  border-radius:14px;
  display:grid;place-items:center;
  font-weight:800;
  letter-spacing:-.02em;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  box-shadow: 0 16px 40px -12px var(--accentGlow);
  user-select:none;
}

.brand-text h1{
  margin:0;
  font-size:18px;
  font-weight:700;
  letter-spacing:-.02em;
}
.brand-text span{
  display:block;
  font-size:12.5px;
  color:var(--muted);
  margin-top:2px;
}

/* --- Utilities --- */
.muted{ color:var(--muted); }
.small{ font-size:12px; color:var(--muted); }
.hr{
  height:1px;
  background:var(--border);
  margin:18px 0;
}

/* --- Cards --- */
.card, .form-card{
  background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.01));
  background-color: var(--card);
  border:1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow2);
  padding:28px;
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
}

.hero{
  padding:36px;
  border:1px solid rgba(88,101,242,.22);
  background:
    radial-gradient(500px 250px at 30% 0%, rgba(88,101,242,.18), transparent 70%),
    linear-gradient(180deg, rgba(88,101,242,.08), transparent 60%);
}

.hero h1{
  margin:0 0 12px;
  font-size:32px;
  line-height:1.08;
  letter-spacing:-.04em;
}
.hero p{
  margin:0 0 18px;
  color:var(--muted);
  font-size:16.5px;
  line-height:1.6;
  max-width:660px;
}

.badge{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:6px 10px;
  border-radius:999px;
  font-size:11px;
  font-weight:800;
  letter-spacing:.08em;
  text-transform:uppercase;
  color:rgba(237,242,247,.9);
  border:1px solid rgba(255,255,255,.10);
  background:rgba(255,255,255,.04);
  margin-bottom:14px;
}

/* Status variants */
.status{
  position:relative;
  overflow:hidden;
}
.status.danger{
  border-color: rgba(239,68,68,.30);
  box-shadow: 0 18px 60px rgba(0,0,0,.45), 0 0 0 1px rgba(239,68,68,.10);
}
.status.danger::before{
  content:"";
  position:absolute; inset:-2px;
  background: radial-gradient(650px 220px at 30% 0%, rgba(239,68,68,.18), transparent 70%);
  pointer-events:none;
}

/* --- Buttons --- */
.btn-row{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
  margin-top:10px;
}

.btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:10px;
  padding:12px 16px;
  border-radius:12px;
  border:1px solid rgba(255,255,255,.10);
  background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02));
  background-color: var(--accent);
  color:white;
  font-weight:700;
  font-size:14px;
  text-decoration:none;
  cursor:pointer;
  transition: transform .15s ease, box-shadow .15s ease, background .15s ease, border-color .15s ease;
  box-shadow: 0 16px 40px -18px var(--accentGlow);
}

.btn:hover{
  transform: translateY(-1px);
  background-color:#4f5ae6;
  box-shadow: 0 22px 50px -22px var(--accentGlow);
}

.btn:active{ transform: translateY(0); }

.btn.secondary{
  background: rgba(255,255,255,.03);
  color: var(--text);
  border-color: rgba(255,255,255,.10);
  box-shadow:none;
}

.btn.secondary:hover{
  background: rgba(255,255,255,.05);
  border-color: rgba(255,255,255,.16);
}

/* --- Inputs --- */
.grid-2{
  display:grid;
  grid-template-columns: 1.2fr .8fr;
  gap:18px;
}
@media (max-width: 900px){
  .grid-2{ grid-template-columns:1fr; }
  .hero{ padding:28px; }
}

.field{ margin-bottom:16px; }
.field label{
  display:block;
  font-size:11px;
  font-weight:800;
  letter-spacing:.08em;
  text-transform:uppercase;
  color: var(--muted2);
  margin-bottom:8px;
}

input[type=text], textarea{
  width:100%;
  border-radius:12px;
  border:1px solid rgba(255,255,255,.10);
  background: rgba(0,0,0,.28);
  color: var(--text);
  padding:13px 14px;
  font-size:14.5px;
  line-height:1.45;
  transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
}

textarea{ min-height:150px; resize:vertical; }

input:focus, textarea:focus{
  outline:none;
  border-color: rgba(88,101,242,.70);
  box-shadow: 0 0 0 4px rgba(88,101,242,.18);
  background: rgba(0,0,0,.36);
}

/* --- Callouts / error boxes --- */
.callout{
  border:1px solid rgba(255,255,255,.10);
  background: rgba(255,255,255,.03);
  border-radius: 14px;
  padding:12px 14px;
  color: var(--muted);
  font-size:13px;
  line-height:1.5;
}

.error-box{
  margin-top:10px;
  border:1px solid rgba(239,68,68,.25);
  background: rgba(239,68,68,.08);
  border-radius:14px;
  padding:12px 14px;
  color: rgba(255,255,255,.92);
  font-size:13px;
  line-height:1.5;
}

/* --- User chip --- */
.user-chip{
  display:flex;
  align-items:center;
  gap:12px;
  padding:8px 12px 8px 8px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.10);
  background: rgba(255,255,255,.03);
}

.user-chip img{
  width:34px;height:34px;
  border-radius:999px;
  object-fit:cover;
  border:1px solid rgba(255,255,255,.10);
  background: rgba(0,0,0,.25);
}

.user-chip .name{
  font-weight:700;
  font-size:13.5px;
}

.user-chip .actions a{
  margin-left:10px;
  font-size:12px;
  color: var(--muted);
  text-decoration:none;
}
.user-chip .actions a:hover{ color: var(--text); }

/* --- History list --- */
.history-list{
  list-style:none;
  padding:0;
  margin:14px 0 0;
  display:flex;
  flex-direction:column;
  gap:10px;
}

.history-item{
  border:1px solid rgba(255,255,255,.08);
  background: rgba(0,0,0,.22);
  border-radius: 14px;
  padding:14px;
  display:grid;
  grid-template-columns: 1fr;
  gap:8px;
}

.history-item .meta{
  font-size:13px;
  color: var(--muted);
  line-height:1.45;
}

.status-chip{
  display:inline-flex;
  width:fit-content;
  align-items:center;
  gap:8px;
  padding:6px 10px;
  border-radius:999px;
  font-size:12px;
  font-weight:800;
  letter-spacing:.02em;
  border:1px solid rgba(255,255,255,.10);
  background: rgba(255,255,255,.03);
}

.status-chip.accepted{ color: rgba(34,197,94,.95); border-color: rgba(34,197,94,.22); background: rgba(34,197,94,.10); }
.status-chip.declined{ color: rgba(239,68,68,.95); border-color: rgba(239,68,68,.22); background: rgba(239,68,68,.10); }
.status-chip.pending{ color: rgba(129,140,248,.95); border-color: rgba(129,140,248,.22); background: rgba(129,140,248,.10); }

/* --- Chat / message context --- */
.chat-box{
  border:1px solid rgba(255,255,255,.08);
  background: rgba(0,0,0,.25);
  border-radius: 14px;
  padding:10px;
  max-height:340px;
  overflow:auto;
}

.chat-row{
  display:grid;
  grid-template-columns: 170px 1fr;
  gap:12px;
  padding:10px 10px;
  border-radius: 12px;
}
.chat-row + .chat-row{ margin-top:6px; }
.chat-row:hover{
  background: rgba(255,255,255,.03);
}

.chat-time{
  font-size:11px;
  color: var(--muted2);
  line-height:1.2;
  white-space:nowrap;
}

.chat-channel{
  display:inline-block;
  margin-top:6px;
  font-size:11px;
  font-weight:800;
  color: rgba(88,101,242,.95);
  border:1px solid rgba(88,101,242,.25);
  background: rgba(88,101,242,.10);
  padding:4px 8px;
  border-radius:999px;
}

.chat-content{
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size:13px;
  line-height:1.5;
  color: rgba(237,242,247,.95);
  word-break:break-word;
}

/* --- Footer --- */
.footer{
  margin-top:8px;
  font-size:12.5px;
  color: rgba(154,164,178,.85);
  opacity:.85;
  text-align:center;
}

:root{
  --bg:#030510;
  --bg-alt:#0f172a;
  --surface:#101828;
  --surface-soft:#141b2e;
  --card-border:rgba(255,255,255,.08);
  --text:#f8fafc;
  --muted:#94a3b8;
  --border:#1f2a44;
  --accent:#6366f1;
  --accent-soft:rgba(99,102,241,.15);
  --radius:18px;
}
body{
  background:radial-gradient(circle at 15% 15%, rgba(99,102,241,.25), transparent 40%), radial-gradient(circle at 85% 0%, rgba(239,68,68,.2), transparent 45%), var(--bg);
}
.app{
  max-width:1100px;
  margin:0 auto;
  padding:32px 24px 48px;
  display:flex;
  flex-direction:column;
  gap:22px;
}
.brand-row{
  display:flex;
  align-items:center;
  justify-content:space-between;
  flex-wrap:wrap;
  gap:16px;
}
.brand{
  display:flex;
  align-items:center;
  gap:10px;
}
.logo{
  width:44px;
  height:44px;
  border-radius:16px;
  display:grid;
  place-items:center;
  font-weight:700;
  letter-spacing:.08em;
  background:linear-gradient(135deg,var(--accent),#8b5cf6);
  color:#fff;
}
.brand-text h1{
  margin:0;
  font-size:20px;
}
.brand-text span{
  font-size:13px;
  color:var(--muted);
}
.card{
  background:var(--surface);
  border:1px solid var(--card-border);
  border-radius:var(--radius);
  box-shadow:0 20px 45px rgba(6,8,15,.4);
  padding:26px;
  display:flex;
  flex-direction:column;
  gap:16px;
}
.hero-card{
  background:linear-gradient(135deg,#11192d,#0f152b 60%);
}
.hero-card h1{
  margin:0;
  font-size:30px;
}
.hero-sub{
  color:var(--muted);
  font-size:17px;
  margin:0;
  max-width:640px;
}
.hero-actions{
  display:flex;
  flex-wrap:wrap;
  gap:12px;
}
.badge{
  padding:5px 14px;
  font-size:11px;
  font-weight:700;
  letter-spacing:.08em;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.2);
  background:rgba(99,102,241,.15);
  color:#e0e7ff;
  width:fit-content;
}
.live-status{
  display:flex;
  align-items:center;
  gap:10px;
  font-size:14px;
  color:var(--muted);
}
.live-status .dot{
  width:10px;
  height:10px;
  border-radius:50%;
  background:#22c55e;
  box-shadow:0 0 12px rgba(34,197,94,.6);
}
.btn{
  padding:12px 22px;
  border-radius:14px;
  border:none;
  font-size:14px;
  font-weight:600;
  cursor:pointer;
  background:var(--accent);
  color:#fff;
  text-decoration:none;
  box-shadow:0 12px 30px rgba(99,102,241,.4);
  transition:transform .2s ease, box-shadow .2s ease;
}
.btn:hover{
  transform:translateY(-1px);
  box-shadow:0 20px 40px rgba(99,102,241,.45);
}
.btn.secondary{
  background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.2);
  color:var(--text);
  box-shadow:none;
}
.info-grid{
  display:grid;
  gap:20px;
  grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
}
.info-card{
  background:var(--surface-soft);
  border:1px solid rgba(255,255,255,.06);
}
.info-card ol{
  margin:0;
  padding-left:18px;
  color:var(--muted);
}
.info-card li{
  margin-bottom:10px;
}
.home-grid{
  display:grid;
  gap:20px;
  grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
  align-items:start;
}
.home-panel{
  display:flex;
  flex-direction:column;
  gap:20px;
}
.history-card{
  background:var(--surface-soft);
  border:1px solid rgba(255,255,255,.08);
  min-height:220px;
}
.history-card h2{
  margin:0;
  font-size:20px;
}
.history-placeholder{
  color:var(--muted);
}
.history-list{
  list-style:none;
  padding:0;
  margin:0;
  display:flex;
  flex-direction:column;
  gap:12px;
}
.history-item{
  border-radius:12px;
  padding:12px 14px;
  border:1px solid rgba(255,255,255,.08);
  background:rgba(255,255,255,.02);
  display:flex;
  flex-direction:column;
  gap:6px;
}
.history-item .meta{
  font-size:13px;
  color:var(--muted);
}
.status-chip{
  padding:4px 10px;
  border-radius:999px;
  font-size:12px;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.05em;
  width:fit-content;
}
.status-chip.accepted{
  background:rgba(34,197,94,.15);
  color:#86efac;
}
.status-chip.declined{
  background:rgba(239,68,68,.15);
  color:#fecdd3;
}
.status-chip.pending{
  background:rgba(99,102,241,.15);
  color:#c7d2fe;
}
.chat-box{
  border:1px solid rgba(255,255,255,.08);
  border-radius:14px;
  background:#0a0d16;
  padding:12px;
  display:flex;
  flex-direction:column;
  gap:10px;
  max-height:280px;
  overflow:auto;
}
.chat-row{
  display:flex;
  flex-direction:column;
  gap:4px;
}
.chat-time{
  font-size:11px;
  color:var(--muted);
  display:flex;
  align-items:center;
  gap:6px;
}
.chat-channel{
  font-size:11px;
  padding:2px 6px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.1);
  color:#cbd5f5;
}
.chat-content{
  font-family:ui-monospace,"SFMono-Regular",Consolas,"Liberation Mono",monospace;
  font-size:14px;
  color:#e2e8f0;
}
.callout{
  border-radius:12px;
  border:1px dashed rgba(255,255,255,.2);
  padding:12px;
  color:var(--muted);
  font-size:13px;
}
.status-card{
  background:var(--surface-soft);
  border:1px solid rgba(255,255,255,.08);
}
.status-heading h1{
  margin:0;
}
.status-heading .muted{
  margin-top:4px;
}
.footer{
  font-size:13px;
  color:var(--muted);
  text-align:center;
  margin-top:30px;
}
@media (max-width:720px){
  .app{
    padding:24px 16px 40px;
  }
  .brand-row{
    flex-direction:column;
    align-items:flex-start;
  }
}

"""

LANG_STRINGS = {
    "en": {
        "hero_title": "Resolve your BlockSpin ban.",
        "hero_sub": "Sign in with Discord to confirm your identity, review ban context, and submit a respectful appeal.",
        "login": "Continue with Discord",
        "how_it_works": "How it works",
        "status_cta": "Track my appeal",
        "history_title": "Appeal history",
        "review_ban": "Review my ban",
        "error_retry": "Retry",
        "error_home": "Go home",
        "ban_details": "Ban details",
        "messages_header": "Recent context",
        "no_messages": "No recent messages available.",
        "language_switch": "Switch language",
    },
    "es": {
        "hero_title": "Resuelve tu baneo en BlockSpin.",
        "hero_sub": "Conecta con Discord, revisa el contexto y envía una apelación clara.",
        "login": "Continuar con Discord",
        "how_it_works": "Como funciona",
        "status_cta": "Ver mi apelacion",
        "history_title": "Historial de apelaciones",
        "review_ban": "Revisar mi baneo",
        "error_retry": "Reintentar",
        "error_home": "Ir al inicio",
        "ban_details": "Detalles del baneo",
        "messages_header": "Contexto reciente",
        "no_messages": "No hay mensajes recientes.",
        "language_switch": "Cambiar idioma",
    },
}
LANG_CACHE: Dict[str, Dict[str, str]] = {}
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

# --- Helpers ---
def normalize_language(lang: Optional[str]) -> str:
    if not lang:
        return "en"
    lang = lang.split(",")[0].split(";")[0].strip().lower()
    if "-" in lang:
        lang = lang.split("-")[0]
    return lang or "en"


def format_timestamp(value: Any) -> str:
    """Convert various timestamp formats to a friendly label."""
    if not value:
        return ""
    try:
        if isinstance(value, str) and "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%b %d, %Y • %H:%M UTC")
    except Exception:
        return str(value)


def hash_value(raw: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{raw}".encode("utf-8", "ignore")).hexdigest()


def hash_ip(ip: str) -> str:
    if not ip or ip == "unknown":
        return "unknown"
    return hash_value(ip)


async def detect_language(request: Request, lang_param: Optional[str] = None) -> str:
    if lang_param:
        return normalize_language(lang_param)
    cookie_lang = request.cookies.get("lang")
    if cookie_lang:
        return normalize_language(cookie_lang)
    accept = request.headers.get("accept-language", "")
    if accept:
        return normalize_language(accept.split(",")[0].strip())
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
    return "en"


async def get_strings(lang: str) -> Dict[str, str]:
    lang = normalize_language(lang)
    base = LANG_STRINGS["en"]
    if lang in LANG_STRINGS:
        return LANG_STRINGS[lang]
    if lang in LANG_CACHE:
        return LANG_CACHE[lang]
    translated: Dict[str, str] = {}
    for key, text in base.items():
        translated[key] = await translate_text(text, target_lang=lang, source_lang="en")
    merged = {**base, **translated}
    LANG_CACHE[lang] = merged
    return merged


async def translate_text(text: str, target_lang: str = "en", source_lang: Optional[str] = None) -> str:
    if not text or normalize_language(target_lang) == "en" and normalize_language(source_lang) == "en":
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
    return text


def clean_display_name(raw: str) -> str:
    if not raw:
        return ""
    if raw.endswith("#0"):
        return raw[:-2]
    return raw


def is_supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def persist_user_session(response: Response, user_id: str, username: str, display_name: Optional[str] = None):
    token = serializer.dumps({"uid": user_id, "uname": username, "iat": time.time(), "display_name": display_name or username})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=PERSIST_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="Lax",
    )


def maybe_persist_session(response: Response, session: Optional[dict], refreshed: bool):
    if session and refreshed:
        persist_user_session(
            response,
            session["uid"],
            session.get("uname") or "",
            display_name=session.get("display_name"),
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


async def refresh_session_profile(session: Optional[dict]) -> Tuple[Optional[dict], bool]:
    if not session:
        return None, False
    user_id = session.get("uid")
    if not user_id:
        return session, False
    token = await get_valid_access_token(str(user_id))
    if not token:
        return session, False
    try:
        user = await fetch_discord_user(token)
    except Exception as exc:
        logging.debug("Profile refresh failed for %s: %s", user_id, exc)
        return session, False
    uname_label = f"{user['username']}#{user.get('discriminator', '0')}"
    display_name = clean_display_name(user.get("global_name") or user.get("username") or uname_label)
    updated = dict(session)
    updated["uname"] = uname_label
    updated["display_name"] = display_name
    updated["iat"] = time.time()
    return updated, True


async def supabase_request(method: str, table: str, *, params: Optional[dict] = None, payload: Optional[dict] = None, prefer: Optional[str] = None):
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
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text or ""
        except Exception:
            body = ""
        logging.warning(
            "Supabase request failed table=%s method=%s status=%s body=%s",
            table,
            method,
            getattr(exc.response, "status_code", "unknown"),
            (body[:800] + "…") if len(body) > 800 else body,
        )
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
    recs = await supabase_request(
        "get",
        SUPABASE_SESSION_TABLE,
        params={"token_hash": f"eq.{token_hash}", "limit": 1},
    )
    return bool(recs)


async def mark_session_token(token_hash: str, user_id: str, ts: float):
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
    payload = {
        "status": status,
        "decision_by": moderator_id,
        "decision_at": int(time.time()),
        "dm_delivered": dm_delivered,
        "notes": notes,
    }
    await supabase_request("patch", SUPABASE_TABLE, params={"appeal_id": f"eq.{appeal_id}"}, payload=payload)


async def fetch_appeal_history(user_id: str, limit: int = 25) -> List[dict]:
    params = {"user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": min(limit, 100)}
    records = await supabase_request("get", SUPABASE_TABLE, params=params)
    return records or []


async def fetch_appeal_record(appeal_id: str) -> Optional[dict]:
    records = await supabase_request(
        "get",
        SUPABASE_TABLE,
        params={"appeal_id": f"eq.{appeal_id}", "limit": 1},
    )
    if records:
        return records[0]
    return None


def render_history_items(history: List[dict]) -> str:
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
        <style>{BASE_STYLES}</style>
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
            {script_block or ""}
        </script>
      </body>
    </html>
    """

def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept


@app.get("/health")
async def health():
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
    return {
        "ok": True,
        "bot_online": online,
        "bot_task": bot_task_state,
        "target_guild_id": TARGET_GUILD_ID,
        "message_cache_guild_ids": sorted(list(MESSAGE_CACHE_GUILD_IDS)) if MESSAGE_CACHE_GUILD_IDS else None,
        "supabase_ready": is_supabase_ready(),
        "supabase_context_table": SUPABASE_CONTEXT_TABLE,
    }


def render_error(title: str, message: str, status_code: int = 400, lang: str = "en", strings: Optional[Dict[str, str]] = None) -> HTMLResponse:
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
    return HTMLResponse(render_page(title, content, lang=lang), status_code=status_code, headers={"Cache-Control": "no-store"})


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
        return render_error("Server error", "Unexpected error. Please try again.", 500, lang=lang, strings=strings)
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


def issue_state_token(ip: str) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    _state_tokens[token] = (ip, now)
    # prune stale tokens (>15 minutes)
    for t, (_, ts) in list(_state_tokens.items()):
        if now - ts > 900:
            _state_tokens.pop(t, None)
    return token


def validate_state_token(token: str, ip: str) -> bool:
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
    now = time.time()
    window_start = now - APPEAL_IP_WINDOW_SECONDS
    if len(_ip_requests) > 10000:
        _ip_requests.clear()
    bucket = _ip_requests.setdefault(ip, [])
    bucket = [t for t in bucket if t >= window_start]
    if len(bucket) >= APPEAL_IP_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down and try again.")
    bucket.append(now)
    _ip_requests[ip] = bucket


async def exchange_code_for_token(code: str) -> dict:
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
    expires_in = float(token_data.get("expires_in") or 0)
    _user_tokens[user_id] = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": time.time() + expires_in - 60 if expires_in else None,
        "token_type": token_data.get("token_type", "Bearer"),
    }


async def refresh_user_token(user_id: str) -> Optional[str]:
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
    token_data = _user_tokens.get(user_id) or {}
    access_token = token_data.get("access_token")
    expires_at = token_data.get("expires_at")
    if not access_token:
        return None
    if expires_at and time.time() > expires_at:
        return await refresh_user_token(user_id)
    return access_token


async def fetch_discord_user(access_token: str) -> dict:
    client = get_http_client()
    resp = await client.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_ban_if_exists(user_id: str) -> Optional[dict]:
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
    client = get_http_client()
    await client.delete(
        f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )


async def remove_from_target_guild(user_id: str) -> Optional[int]:
    client = get_http_client()
    resp = await client.delete(
        f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
    )
    if resp.status_code not in (200, 204, 404):
        logging.warning("Failed to remove user %s from guild %s: %s %s", user_id, TARGET_GUILD_ID, resp.status_code, resp.text)
    return resp.status_code


async def add_user_to_guild(user_id: str, guild_id: str) -> Optional[int]:
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
    """Send a plaintext log line to the auth/ops channel."""
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
    if not is_supabase_ready():
        return
    if not should_track_messages(guild_id):
        logging.debug("Message caching skipped for guild %s", guild_id)
        return
    entries = list(_message_buffer.get(user_id, []))
    if not entries:
        return
    if BOT_EVENT_LOGGING and DEBUG_EVENTS:
        logging.info("[snapshot] user=%s guild=%s msgs=%s", user_id, guild_id, len(entries[-15:]))
    await persist_message_snapshot(user_id, entries[-15:])


async def persist_message_snapshot(user_id: str, messages: List[dict]):
    if not is_supabase_ready() or not messages:
        return
    logging.info("Persisting %d messages for user %s", len(messages[-15:]), user_id)
    try:
        await supabase_request(
            "post",
            "user_message_snapshots",
            params={"on_conflict": "user_id"},
            payload={"user_id": user_id, "messages": messages[-15:]},
            prefer="resolution=merge-duplicates,return=representation",
        )
    except Exception as exc:
        logging.warning("Snapshot persist failed for %s: %s", user_id, exc)

async def fetch_message_cache(user_id: str, limit: int = 15) -> List[dict]:
    """Fetch ban context from the permanent ban log in Supabase."""
    if not is_supabase_ready():
        return _get_recent_message_context(user_id, limit)
    try:
        recs = await supabase_request(
            "get",
            SUPABASE_CONTEXT_TABLE,
            params={"user_id": f"eq.{user_id}", "limit": 1},
        )
        if recs and recs[0].get("messages"):
            messages = recs[0]["messages"]

            def get_ts(m: dict) -> float:
                t = m.get("timestamp", 0)
                try:
                    return float(t)
                except Exception:
                    return 0.0

            return sorted(messages, key=get_ts, reverse=True)[:limit]
    except Exception as exc:
        logging.warning("Failed to fetch context for %s: %s", user_id, exc)
    try:
        recs = await supabase_request(
            "get",
            "user_message_snapshots",
            params={"user_id": f"eq.{user_id}", "limit": 1},
        )
        if recs and recs[0].get("messages"):
            messages = recs[0]["messages"]

            def get_ts(m: dict) -> float:
                t = m.get("timestamp", 0)
                try:
                    return float(t)
                except Exception:
                    return 0.0

            return sorted(messages, key=get_ts, reverse=True)[:limit]
    except Exception:
        pass
    return _get_recent_message_context(user_id, limit)


def _get_recent_message_context(user_id: str, limit: int) -> List[dict]:
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
    client = get_http_client()
    resp = await client.post(
        f"{DISCORD_API_BASE}/channels/{APPEAL_CHANNEL_ID}/messages",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        json={"embeds": [embed], "components": components},
    )
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Discord is rate limiting. Please retry in a minute.")
    resp.raise_for_status()


# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, lang: Optional[str] = None):
    current_lang = await detect_language(request, lang)
    strings = await get_strings(current_lang)
    ip = get_client_ip(request)
    state_token = issue_state_token(ip)
    state = serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang, "state_id": state_token})
    asyncio.create_task(send_log_message(f"[visit_home] ip_hash={hash_ip(ip)} lang={current_lang}"))
    user_session = read_user_session(request)
    user_session, session_refreshed = await refresh_session_profile(user_session)
    user_chip = build_user_chip(user_session)
    strings = dict(strings)
    strings["user_chip"] = user_chip
    history_html = ""
    if user_session and is_supabase_ready():
        history = await fetch_appeal_history(user_session["uid"], limit=5)
        history_html = render_history_items(history)
    elif user_session:
        history_html = "<div class='muted'></div>"

    login_url = oauth_authorize_url(state)
    primary_action = (
        f'<a class="btn" href="/status">{strings["review_ban"]}</a>'
        if user_session
        else f'<a class="btn" href="{login_url}">{strings["login"]}</a>'
    )
    secondary_action = f'<a class="btn secondary" href="/status">{strings["status_cta"]}</a>'

    if user_session:
        history_panel = f"""
        <div class="card history-card">
          <h2>{strings['history_title']}</h2>
          <div id="live-history">{history_html}</div>
        </div>
        """
    else:
        history_panel = f"""
        <div class="card history-card">
          <h2>{strings['history_title']}</h2>
          <p class="history-placeholder">Sign in to review your appeal activity.</p>
          <div class="btn-row" style="margin-top:6px;">
            <a class="btn secondary" href="/status">{strings['status_cta']}</a>
          </div>
        </div>
        """

    content = f"""
      <div class="home-grid">
        <div class="home-panel">
          <div class="card hero-card">
            <div class="hero-meta">
              <div>
                <div class="badge">Official portal</div>
                <p class="hero-sub" style="margin:8px 0 0;">{strings['hero_sub']}</p>
              </div>
              <div id="live-status" class="live-status">
                <span class="dot"></span>
                <span class="value">System online</span>
              </div>
            </div>
            <h1>{strings['hero_title']}</h1>
            <div class="hero-actions">
              {primary_action}
              {secondary_action}
            </div>
          </div>
          <div class="card info-card">
            <h2>{strings.get('how_it_works', 'How it works')}</h2>
            <ol>
              <li>Authenticate with Discord so we can verify the account.</li>
              <li>Review the ban details and any message context before composing your appeal.</li>
              <li>Describe what happened, share supporting evidence, and wait for moderator feedback.</li>
            </ol>
          </div>
        </div>
        {history_panel}
      </div>
    """
    script_nonce = secrets.token_urlsafe(12)
    strings["script_nonce"] = script_nonce
    history_poll = ""
    if user_session:
        history_poll = """
      const historyEl = document.getElementById('live-history');
      async function loadHistory() {
        if (!historyEl) return;
        try {
          const res = await fetch('/status/data', { headers: { 'Accept': 'application/json' }});
          if (!res.ok) throw new Error('status ' + res.status);
          const data = await res.json();
          const history = data.history || [];
          if (!history.length) {
            historyEl.innerHTML = "<div class='muted'>No appeals yet.</div>";
            return;
          }
          const rows = history.map(item => {
            const status = (item.status || 'pending').toLowerCase();
            const statusClass = status.startsWith('accept') ? 'accepted' : status.startsWith('decline') ? 'declined' : 'pending';
            const created = item.created_at || '';
            const safeRef = (item.appeal_id || '').replace(/</g,'&lt;');
            const safeBan = (item.ban_reason || '').replace(/</g,'&lt;');
            return `
              <li class="history-item">
                <div class="status-chip ${statusClass}">${status.charAt(0).toUpperCase()+status.slice(1)}</div>
                <div class="meta">Reference: ${safeRef || '-'}</div>
                <div class="meta">Submitted: ${created}</div>
                <div class="meta">Ban reason: ${safeBan}</div>
              </li>
            `;
          }).join('');
          historyEl.innerHTML = `<ul class='history-list'>${rows}</ul>`;
        } catch (e) {
          // leave existing content
        }
      }
      loadHistory();
      setInterval(loadHistory, 20000);
    """

    strings["script_block"] = f"""
    (function() {{
      const el = document.getElementById('live-status');
      if (!el) return;
      const valEl = el.querySelector('.value');
      async function tick() {{
        try {{
          const res = await fetch('/status/data', {{ headers: {{ 'Accept': 'application/json' }} }});
          if (!res.ok) throw new Error('status ' + res.status);
          const data = await res.json();
          const history = data.history || [];
          if (!history.length) {{
            valEl.textContent = 'No appeals yet.';
            return;
          }}
          const latest = history[0];
          const status = latest.status || 'pending';
          const ref = latest.appeal_id || 'n/a';
          valEl.textContent = status + ' • ref ' + ref;
        }} catch (e) {{
          valEl.textContent = 'Live updates unavailable.';
        }}
      }}
      tick();
      setInterval(tick, 15000);
      {history_poll}
    }})();
    """
    # Override script block to ensure clean characters in live status text
    strings["script_block"] = f"""
    (function() {{
      const el = document.getElementById('live-status');
      if (!el) return;
      const valEl = el.querySelector('.value');
      async function tick() {{
        try {{
          const res = await fetch('/status/data', {{ headers: {{ 'Accept': 'application/json' }} }});
          if (!res.ok) throw new Error('status ' + res.status);
          const data = await res.json();
          const history = data.history || [];
          if (!history.length) {{
            valEl.textContent = 'No appeals yet.';
            return;
          }}
          const latest = history[0];
          const status = latest.status || 'pending';
          const ref = latest.appeal_id || 'n/a';
          valEl.textContent = status + ' • ref ' + ref;
        }} catch (e) {{
          valEl.textContent = 'Live updates unavailable.';
        }}
      }}
      tick();
      setInterval(tick, 15000);
      {history_poll}
    }})();
    """
    response = HTMLResponse(render_page("BlockSpin Appeals", content, lang=current_lang, strings=strings), headers={"Cache-Control": "no-store"})
    maybe_persist_session(response, user_session, session_refreshed)
    response.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
    return response


@app.get("/tos", response_class=HTMLResponse)
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


@app.get("/privacy", response_class=HTMLResponse)
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


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, lang: Optional[str] = None):
    current_lang = await detect_language(request, lang)
    strings = await get_strings(current_lang)
    ip = get_client_ip(request)
    asyncio.create_task(send_log_message(f"[visit_status] ip_hash={hash_ip(ip)} lang={current_lang}"))
    session = read_user_session(request)
    session, session_refreshed = await refresh_session_profile(session)
    strings = dict(strings)
    strings["user_chip"] = build_user_chip(session)
    if not session:
        login_url = oauth_authorize_url(serializer.dumps({"nonce": secrets.token_urlsafe(8), "lang": current_lang}))
        content = f"""
          <div class="card status danger">
            <h1 style="margin-bottom:10px;">Sign in required</h1>
            <p class="muted">Sign in to view your BlockSpin appeal history and live status.</p>
            <a class="btn" href="{login_url}">{strings['login']}</a>
          </div>
        """
        resp = HTMLResponse(render_page("Appeal status", content, lang=current_lang, strings=strings), status_code=401, headers={"Cache-Control": "no-store"})
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
        return resp

    history_html = ""
    if is_supabase_ready():
        history = await fetch_appeal_history(session["uid"], limit=10)
        history_html = render_history_items(history)
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


@app.get("/status/data")
async def status_data(request: Request):
    session = read_user_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not is_supabase_ready():
        return {"history": []}
    history = await fetch_appeal_history(session["uid"], limit=5)
    slim = [
        {
            "appeal_id": item.get("appeal_id"),
            "status": item.get("status"),
            "created_at": format_timestamp(item.get("created_at")),
            "ban_reason": item.get("ban_reason"),
        }
        for item in history
    ]
    return {"history": slim}


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/callback")
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
    # Log authorization with network details
    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    state_id = state_data.get("state_id")
    if not validate_state_token(state_id, ip):
        raise HTTPException(status_code=400, detail="Invalid or replayed state")
    asyncio.create_task(
        send_log_message(
            f"[auth] user={user['id']} ip_hash={hash_ip(ip)} lang={current_lang}"
        )
    )

    history_html = ""
    if is_supabase_ready():
        history = await fetch_appeal_history(user["id"])
        history_html = render_history_items(history)
    else:
        history_html = "<div class='muted'></div>"

    strings = dict(strings)
    strings["user_chip"] = build_user_chip({"display_name": display_name, "uname": uname_label})

    def respond(body_html: str, title: str, status_code: int = 200) -> HTMLResponse:
        resp = HTMLResponse(render_page(title, body_html, lang=current_lang, strings=strings), status_code=status_code, headers={"Cache-Control": "no-store"})
        persist_user_session(resp, user["id"], uname_label, display_name=display_name)
        resp.set_cookie("lang", current_lang, max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax")
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

    message_cache = await fetch_message_cache(user["id"])

    # Fallback: if the ban event wasn't observed (e.g. bot restart), persist the best-available cache when the banned
    # user authenticates so moderators can still see context in Supabase.
    if is_supabase_ready() and message_cache:
        logging.info(
            "Upserting banned context from callback user=%s msgs=%s table=%s",
            user["id"],
            len(message_cache),
            SUPABASE_CONTEXT_TABLE,
        )
        await supabase_request(
            "post",
            SUPABASE_CONTEXT_TABLE,
            params={"on_conflict": "user_id"},
            payload={
                "user_id": user["id"],
                "messages": message_cache,
                "banned_at": int(time.time()),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )

    session = serializer.dumps(
        {
            "uid": user["id"],
            "uname": f"{user['username']}#{user.get('discriminator','0')}",
            "ban_reason": ban.get("reason") or "No reason provided.",
            "iat": time.time(),
            "ban_first_seen": first_seen,
            "lang": current_lang,
            "message_cache": message_cache,
        }
    )
    uname = html.escape(f"{user['username']}#{user.get('discriminator','0')}")
    ban_reason = html.escape(ban.get("reason") or "No reason provided.")
    cooldown_minutes = max(1, APPEAL_COOLDOWN_SECONDS // 60)
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
    content = f"""
      <div class="grid-2">
        <div class="form-card">
          <div class="badge">Window: {max(1, window_remaining // 60)} minutes left</div>
          <h2 style="margin:8px 0;">Appeal your BlockSpin ban</h2>
          <p class="muted">One appeal per ban. Include context, evidence, and what you will change.</p>
          <form class="form" action="/submit" method="post">
            <input type="hidden" name="session" value="{html.escape(session)}" />
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
           <div class="callout" style="margin-top:10px;">Share concise context and relevant evidence so moderators can review your appeal efficiently.</div>
        </div>
        <div class="card">
          <h2>{strings['ban_details']}</h2>
          <p class="muted"><strong>User:</strong> {uname}</p>
          <p class="muted"><strong>Ban reason:</strong> {ban_reason}</p>
          <div style="margin-top:12px;">
            <h3 style="margin:0 0 6px;">{strings['messages_header']}</h3>
            {message_cache_html}
          </div>
          <div style="margin-top:12px;">
            <h3 style="margin:0 0 6px;">Your history</h3>
            {history_html}
          </div>
          <div class="btn-row" style="margin-top:10px;">
            <a class="btn secondary" href="/">Back home</a>
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

    if len(appeal_reason or "") > 2000:
        raise HTTPException(status_code=400, detail="Appeal reason too long. Please keep it under 2000 characters.")
    if len(evidence or "") > 1500:
        raise HTTPException(status_code=400, detail="Evidence too long. Please keep it concise.")

    token_hash = hash_value(session)
    # Session expiry + single-use guard
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

    # Per-IP throttle to slow basic spam
    ip = get_client_ip(request)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    user_agent = request.headers.get("User-Agent", "unknown")
    enforce_ip_rate_limit(ip)
    asyncio.create_task(
        send_log_message(
            f"[appeal_attempt] user={data.get('uid')} ip_hash={hash_ip(ip)}"
        )
    )

    # Rate limit to prevent spam (persisted)
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

    # Persist audit trail to Supabase (best effort)
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

    token_hash = hash_value(session)
    _used_sessions[token_hash] = now
    await mark_session_token(token_hash, user["id"], now)
    _appeal_locked[data["uid"]] = True
    # prune old used sessions
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
    client = get_http_client()
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
    add_status: Optional[int] = None,
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
    if add_status:
        fields.append({"name": "Guild add", "value": str(add_status), "inline": True})
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

        # Prepare UI update embed (buttons removed) to reflect real outcome
        def updated_embed(status: str, note: Optional[str] = None) -> dict:
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
            if note:
                embed["fields"].append({"name": "Notes", "value": note, "inline": False})
            return embed

        async def handle_accept() -> Tuple[Optional[dict], Optional[str]]:
            if appeal_id in _processed_appeals:
                return None, "Appeal already processed."
            _processed_appeals[appeal_id] = time.time()
            try:
                appeal_record = await fetch_appeal_record(appeal_id)
                user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))

                client = get_http_client()
                unban_resp = await client.delete(
                    f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                )
                unban_status = unban_resp.status_code
                if unban_status not in (200, 204, 404):
                    raise RuntimeError(f"Unban failed with status {unban_status}")

                removal_status = await remove_from_target_guild(user_id)
                readd_status = await add_user_to_guild(user_id, READD_GUILD_ID)

                accept_desc_en = (
                    "Your appeal has been reviewed and accepted. You have been unbanned and re-added to the server."
                )
                accept_desc = (
                    await translate_text(accept_desc_en, target_lang=user_lang, source_lang="en")
                    if user_lang != "en"
                    else accept_desc_en
                )
                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Accepted",
                        "description": accept_desc,
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
                    add_status=readd_status,
                )

                await client.post(
                    f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={"embeds": [log_embed]},
                )

                note = []
                if unban_status not in (200, 204):
                    note.append(f"Unban status: {unban_status}")
                if removal_status and removal_status not in (200, 204, 404):
                    note.append(f"Removal status: {removal_status}")
                if readd_status and readd_status not in (200, 201, 204):
                    note.append(f"Re-add status: {readd_status}")
                if not dm_delivered:
                    note.append("DM delivery failed")
                note_text = "; ".join(note) if note else None
                return updated_embed("accepted", note_text), None
            except Exception as exc:  # log for debugging
                _processed_appeals.pop(appeal_id, None)
                logging.exception("Failed to process acceptance for appeal %s: %s", appeal_id, exc)
                return None, "Unable to accept appeal. Check bot permissions and try again."

        async def handle_decline() -> Tuple[Optional[dict], Optional[str]]:
            if appeal_id in _processed_appeals:
                return None, "Appeal already processed."
            _processed_appeals[appeal_id] = time.time()
            try:
                _declined_users[user_id] = True
                _appeal_locked[user_id] = True

                appeal_record = await fetch_appeal_record(appeal_id)
                user_lang = normalize_language((appeal_record or {}).get("user_lang", "en"))

                removal_status = await remove_from_target_guild(user_id)

                decline_desc_en = (
                    "Your appeal has been reviewed and declined. Further appeals are blocked for this ban.\n"
                    "You have been removed from the guild for security."
                )
                decline_desc = (
                    await translate_text(decline_desc_en, target_lang=user_lang, source_lang="en")
                    if user_lang != "en"
                    else decline_desc_en
                )
                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Declined",
                        "description": decline_desc,
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

                client = get_http_client()
                await client.post(
                    f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={"embeds": [log_embed]},
                )

                await maybe_remove_from_dm_guild(user_id)

                note = []
                if removal_status and removal_status not in (200, 204, 404):
                    note.append(f"Removal status: {removal_status}")
                if not dm_delivered:
                    note.append("DM delivery failed")
                return updated_embed("declined", "; ".join(note) if note else None), None
            except Exception as exc:  # log for debugging
                _processed_appeals.pop(appeal_id, None)
                logging.exception("Failed to process decline for appeal %s: %s", appeal_id, exc)
                return None, "Unable to decline appeal right now."

        if action == "web_appeal_accept":
            embed, error = await handle_accept()
            if error:
                return await respond_ephemeral_embed("Action failed", error)
            return JSONResponse(
                {
                    "type": 7,
                    "data": {"embeds": [embed], "components": []},
                }
            )

        if action == "web_appeal_decline":
            embed, error = await handle_decline()
            if error:
                return await respond_ephemeral_embed("Action failed", error)
            return JSONResponse(
                {
                    "type": 7,
                    "data": {"embeds": [embed], "components": []},
                }
            )

    return JSONResponse({"type": 4, "data": {"content": "Unsupported interaction", "flags": 1 << 6}})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
