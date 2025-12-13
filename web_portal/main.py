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
# simple in-memory stores
_appeal_rate_limit: Dict[str, float] = {}  # {user_id: timestamp_of_last_submit}
_used_sessions: Dict[str, float] = {}  # {session_token: timestamp_used}
_ip_requests: Dict[str, List[float]] = {}  # {ip: [timestamps]}
_ban_first_seen: Dict[str, float] = {}  # {user_id: first time we saw the ban}
_appeal_locked: Dict[str, bool] = {}  # {user_id: True if appealed already}
_user_tokens: Dict[str, str] = {}  # {user_id: last OAuth access token}
_processed_appeals: Dict[str, float] = {}  # {appeal_id: timestamp_processed}
APPEAL_COOLDOWN_SECONDS = int(os.getenv("APPEAL_COOLDOWN_SECONDS", "300"))  # 5 minutes by default
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "900"))  # sessions expire after 15 minutes
APPEAL_IP_MAX_REQUESTS = int(os.getenv("APPEAL_IP_MAX_REQUESTS", "8"))
APPEAL_IP_WINDOW_SECONDS = int(os.getenv("APPEAL_IP_WINDOW_SECONDS", "60"))
APPEAL_WINDOW_SECONDS = int(os.getenv("APPEAL_WINDOW_SECONDS", str(7 * 24 * 3600)))  # 7 days default
DM_GUILD_ID = os.getenv("DM_GUILD_ID")  # optional: holding guild to enable DMs
REMOVE_FROM_DM_GUILD_AFTER_DM = os.getenv("REMOVE_FROM_DM_GUILD_AFTER_DM", "true").lower() == "true"

BASE_STYLES = """
:root {
  --bg: #0b0f15;
  --card: #101621;
  --border: #1b2432;
  --text: #e6eaf2;
  --muted: #9aa3b5;
  --accent: #5b7bff;
  --accent-2: #4ad6a7;
  --danger: #f87272;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px 16px;
  min-height: 100vh;
  font-family: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  align-items: center;
  justify-content: center;
}
.shell {
  width: 100%;
  max-width: 520px;
  padding: 0 20px;
}
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 26px;
  text-align: center;
  box-shadow: 0 24px 60px rgba(0,0,0,0.35);
}
h1 { font-size: 1.7rem; margin: 0 0 12px; font-weight: 700; letter-spacing: -0.01em; }
p { font-size: 0.97rem; line-height: 1.6; margin: 0 0 18px; color: var(--muted); }
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  background: linear-gradient(120deg, var(--accent), #4b66e6);
  color: #fff;
  text-decoration: none;
  font-weight: 600;
  padding: 12px 16px;
  border-radius: 10px;
  border: 1px solid transparent;
  transition: background 0.12s ease, transform 0.12s ease;
}
.btn:hover { background: #4357c9; transform: translateY(-1px); }
.footer { margin-top: 16px; font-size: 0.82rem; color: var(--muted); }
.list { text-align: left; padding-left: 18px; color: var(--muted); margin: 0 0 14px; }
.field { text-align: left; margin-bottom: 14px; }
.field label { display: block; font-weight: 600; margin-bottom: 6px; }
input[type=text], textarea {
  width: 100%;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: #0f1217;
  color: var(--text);
  padding: 11px;
  font-size: 0.95rem;
}
textarea { resize: vertical; min-height: 120px; }
.status { margin-top: 12px; padding: 12px; border-radius: 10px; border: 1px solid var(--border); background: #0f1217; }
.status.danger { border-color: rgba(248,113,113,0.35); color: var(--danger); }
"""


# --- Helpers ---
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
    token = _user_tokens.get(user_id)
    if not token:
        return False
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"access_token": token},
        )
    return resp.status_code in (200, 201, 204)


async def maybe_remove_from_dm_guild(user_id: str):
    if not DM_GUILD_ID or not REMOVE_FROM_DM_GUILD_AFTER_DM:
        return
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{DISCORD_API_BASE}/guilds/{DM_GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )


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
async def home():
    state = serializer.dumps({"nonce": secrets.token_urlsafe(8)})
    content = f"""
      <div class="card">
        <h1>Ban Appeal</h1>
        <p>Sign in with your Discord account to view your ban details and submit one appeal within 7 days.</p>
        <a class="btn" href="{oauth_authorize_url(state)}">
          <svg viewBox="0 0 245 240" fill="currentColor" width="20" height="20" aria-hidden="true">
            <path d="M104.4 104.8c-5.7 0-10.2 5-10.2 11.1 0 6.1 4.6 11.1 10.2 11.1 5.7 0 10.3-5 10.2-11.1 0-6.1-4.5-11.1-10.2-11.1zm36.2 0c-5.7 0-10.2 5-10.2 11.1 0 6.1 4.6 11.1 10.2 11.1 5.7 0 10.3-5 10.2-11.1 0-6.1-4.5-11.1-10.2-11.1z"/>
            <path d="M189.5 20h-134C24.5 20 10 34.5 10 52.4v135.1c0 17.9 14.5 32.4 32.4 32.4h113.2l-5.3-18.5 12.8 11.9 12.1 11.2 21.5 19V52.4c0-17.9-14.5-32.4-32.4-32.4z"/>
          </svg>
          Login with Discord
        </a>
        <div class="footer">You will be redirected to Discord for authentication.</div>
      </div>
    """
    return HTMLResponse(render_page("BlockSpin Appeals", content), headers={"Cache-Control": "no-store"})


@app.get("/callback")
async def callback(code: str, state: str):
    try:
        serializer.loads(state)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid state")

    token = await exchange_code_for_token(code)
    user = await fetch_discord_user(token["access_token"])
    _user_tokens[user["id"]] = token["access_token"]
    # Try to place the user into a DM-capable guild so we can message later.
    await ensure_dm_guild_membership(user["id"])
    ban = await fetch_ban_if_exists(user["id"])

    if not ban:
        content = f"""
          <div class="card status">
            <p>No active ban found for {html.escape(user['username'])}#{html.escape(user.get('discriminator','0'))}.</p>
            <a class="btn" href="/">Back home</a>
          </div>
        """
        return HTMLResponse(render_page("No active ban", content), status_code=200, headers={"Cache-Control": "no-store"})

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
        return HTMLResponse(render_page("Appeal window closed", expired), status_code=403, headers={"Cache-Control": "no-store"})

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
        return HTMLResponse(render_page("Appeal already submitted", blocked), status_code=409, headers={"Cache-Control": "no-store"})

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
      <div class="card" style="text-align:left;">
        <p><strong>User:</strong> {uname}</p>
        <p><strong>Ban reason:</strong> {ban_reason}</p>
        <p class="muted">One appeal per ban. Time remaining: {max(1, window_remaining // 60)} minutes.</p>
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
      </div>
    """
    return HTMLResponse(
        render_page("Appeal your ban", content),
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


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


async def update_appeal_message(channel_id: str, message_id: str, old_embed: dict, status: str, moderator_id: str):
    """
    Edits the original appeal message to remove buttons and update status.
    """
    new_embed = copy.deepcopy(old_embed or {})

    if status == "accepted":
        color = 0x2ECC71  # Green
        title_suffix = " (ACCEPTED)"
    else:
        color = 0xE74C3C  # Red
        title_suffix = " (DECLINED)"

    new_embed["color"] = color
    new_embed["title"] = new_embed.get("title", "Appeal") + title_suffix

    # Add a field showing who handled it
    new_embed["fields"] = new_embed.get("fields", []) + [
        {"name": "Action Taken", "value": f"{status.title()} by <@{moderator_id}>", "inline": False}
    ]

    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={
                "embeds": [new_embed],
                "components": [],  # Removes the buttons
            },
        )


@app.post("/interactions")
async def interactions(request: Request):
    body = await request.body()
    if not verify_signature(request, body):
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    payload = await request.json()
    if payload["type"] == 1:  # PING
        return JSONResponse({"type": 1})

    if payload["type"] == 3:  # COMPONENT
        data = payload.get("data", {})
        custom_id = data.get("custom_id", "")
        member = payload.get("member") or {}
        user_obj = member.get("user") or {}
        moderator_id = user_obj.get("id")

        # Check permissions
        roles = set(map(int, member.get("roles", [])))
        if MODERATOR_ROLE_ID not in roles:
            return await respond_ephemeral_embed(
                "Not allowed",
                "You don’t have the moderator role required to handle appeals.",
                0xE74C3C,
            )

        try:
            action, appeal_id, user_id = custom_id.split(":")
        except ValueError:
            return await respond_ephemeral_embed("Invalid request", "Bad interaction payload.")

        # Extract message details for editing later
        channel_id = payload["channel_id"]
        message_id = payload["message"]["id"]
        embeds = payload["message"].get("embeds") or []
        original_embed = copy.deepcopy(embeds[0]) if embeds else {}

        # Basic replay/spam guard: ignore if custom_id format looks wrong or missing ids
        if not appeal_id or not user_id or action not in {"web_appeal_accept", "web_appeal_decline"}:
            return await respond_ephemeral_embed("Invalid request", "Malformed interaction data.")

        # Run the heavy work in background to avoid interaction timeouts.
        async def handle_accept():
            try:
                # idempotency: ignore double clicks / retries
                if appeal_id in _processed_appeals:
                    return
                _processed_appeals[appeal_id] = time.time()

                async with httpx.AsyncClient() as client:
                    unban_resp = await client.delete(
                        f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    )

                # Update the original appeal message (REMOVE BUTTONS + STATUS)
                await update_appeal_message(channel_id, message_id, original_embed, "accepted", moderator_id)

                # DM user (best effort)
                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Accepted",
                        "description": "Your appeal has been reviewed and accepted. You have been unbanned.",
                        "color": 0x2ECC71,
                    },
                )

                # Log once
                log_content = f"Appeal `{appeal_id}` **ACCEPTED** by <@{moderator_id}>. "
                if unban_resp.status_code in (200, 204):
                    log_content += f"User <@{user_id}> unbanned."
                elif unban_resp.status_code == 404:
                    log_content += f"User <@{user_id}> was not banned (404)."
                else:
                    log_content += f"Unban API returned {unban_resp.status_code}."

                if not dm_delivered:
                    log_content += " (DM failed)."

                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                        json={"content": log_content},
                    )
            except Exception as exc:  # log for debugging
                logging.exception("Failed to process acceptance for appeal %s: %s", appeal_id, exc)

        async def handle_decline():
            try:
                if appeal_id in _processed_appeals:
                    return
                _processed_appeals[appeal_id] = time.time()

                dm_delivered = await dm_user(
                    user_id,
                    {
                        "title": "Appeal Declined",
                        "description": "Your appeal has been reviewed and declined.",
                        "color": 0xE74C3C,
                    },
                )

                await update_appeal_message(channel_id, message_id, original_embed, "declined", moderator_id)

                log_content = f"Appeal `{appeal_id}` **DECLINED** by <@{moderator_id}>."
                if not dm_delivered:
                    log_content += " (DM failed)."

                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                        json={"content": log_content},
                    )
            except Exception as exc:  # log for debugging
                logging.exception("Failed to process decline for appeal %s: %s", appeal_id, exc)

        if action == "web_appeal_accept":
            asyncio.create_task(handle_accept())
            return JSONResponse({"type": 6})  # DEFERRED_UPDATE_MESSAGE

        if action == "web_appeal_decline":
            asyncio.create_task(handle_decline())
            return JSONResponse({"type": 6})  # DEFERRED_UPDATE_MESSAGE

    return JSONResponse({"type": 4, "data": {"content": "Unsupported interaction", "flags": 1 << 6}})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
