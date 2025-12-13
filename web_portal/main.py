import os
import secrets
import uuid
import asyncio
import time
import html
from typing import Optional, Tuple, Dict, List

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from itsdangerous import URLSafeSerializer, BadSignature
from dotenv import load_dotenv

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

OAUTH_SCOPES = "identify"
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
serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")
# simple in-memory stores
_appeal_rate_limit: Dict[str, float] = {}  # {user_id: timestamp_of_last_submit}
_used_sessions: Dict[str, float] = {}  # {session_token: timestamp_used}
_ip_requests: Dict[str, List[float]] = {}  # {ip: [timestamps]}
APPEAL_COOLDOWN_SECONDS = int(os.getenv("APPEAL_COOLDOWN_SECONDS", "300"))  # 5 minutes by default
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "900"))  # sessions expire after 15 minutes
APPEAL_IP_MAX_REQUESTS = int(os.getenv("APPEAL_IP_MAX_REQUESTS", "8"))
APPEAL_IP_WINDOW_SECONDS = int(os.getenv("APPEAL_IP_WINDOW_SECONDS", "60"))

BASE_STYLES = """
:root {
  --bg: #0b1021;
  --card: #121a35;
  --accent: #6cd4ff;
  --accent-2: #4bd1a0;
  --text: #e8edff;
  --muted: #9fb0d9;
  --danger: #ff6b6b;
  --shadow: 0 20px 60px rgba(0,0,0,0.45);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0;
  min-height: 100vh;
  font-family: "Segoe UI", "SF Pro Display", system-ui, -apple-system, sans-serif;
  background: radial-gradient(circle at 20% 20%, rgba(108,212,255,0.12), transparent 25%),
              radial-gradient(circle at 80% 0%, rgba(75,209,160,0.10), transparent 22%),
              var(--bg);
  color: var(--text);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
}
.shell {
  width: min(960px, 95vw);
  background: linear-gradient(145deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 18px;
  padding: 28px;
  box-shadow: var(--shadow);
  backdrop-filter: blur(10px);
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.badge {
  padding: 6px 12px;
  border-radius: 999px;
  background: rgba(108,212,255,0.12);
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid rgba(108,212,255,0.3);
}
.hero {
  display: grid;
  gap: 14px;
}
.title {
  font-size: 28px;
  margin: 0;
  letter-spacing: -0.02em;
}
.subtitle {
  margin: 0;
  color: var(--muted);
  line-height: 1.5;
}
.card {
  background: var(--card);
  border-radius: 14px;
  padding: 18px;
  border: 1px solid rgba(255,255,255,0.04);
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 12px;
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 18px;
  border-radius: 12px;
  font-weight: 700;
  border: 1px solid rgba(255,255,255,0.08);
  color: var(--text);
  text-decoration: none;
  background: linear-gradient(120deg, var(--accent), #5bb6ff);
  box-shadow: 0 10px 30px rgba(108,212,255,0.35);
  transition: transform 120ms ease, box-shadow 120ms ease, filter 120ms ease;
}
.btn:hover { transform: translateY(-1px); filter: brightness(1.05); }
.btn.secondary {
  background: linear-gradient(120deg, var(--card), rgba(255,255,255,0.04));
  box-shadow: none;
  color: var(--text);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.muted { color: var(--muted); font-size: 14px; }
.list { margin: 0; padding-left: 18px; color: var(--muted); }
.list li { margin-bottom: 6px; }
.form {
  display: grid;
  gap: 12px;
}
.field {
  display: grid;
  gap: 6px;
}
.field label { font-weight: 600; color: var(--text); }
input[type=text], textarea {
  width: 100%;
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,0.08);
  background: rgba(255,255,255,0.04);
  color: var(--text);
  padding: 12px;
  font-size: 15px;
}
textarea { resize: vertical; min-height: 140px; }
.status {
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,0.08);
  background: rgba(108,212,255,0.08);
  color: var(--text);
}
.status.danger { background: rgba(255,107,107,0.12); color: #ffc0c0; }
.pill { padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,0.05); font-size: 13px; }
.stack { display: grid; gap: 10px; }
.footer { margin-top: 10px; font-size: 13px; color: var(--muted); }
"""


# --- Helpers ---
def render_page(title: str, body_html: str) -> str:
    return f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{html.escape(title)}</title>
        <style>{BASE_STYLES}</style>
      </head>
      <body>
        <div class="shell">
          {body_html}
        </div>
      </body>
    </html>
    """


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
      <div class="header">
        <div class="brand">
          <div class="badge">BlockSpin Appeals</div>
          <div class="pill">Secure by design</div>
        </div>
        <span class="muted">Fast review pipeline for moderators</span>
      </div>
      <div class="card hero">
        <div class="stack">
          <h1 class="title">Appeal your server ban</h1>
          <p class="subtitle">
            Authenticate with Discord so we know it is you. Your appeal is sent straight to the BlockSpin
            moderation team with anti-spam protection and a unique reference.
          </p>
          <div class="actions">
            <a class="btn" href="{oauth_authorize_url(state)}">Continue with Discord</a>
            <span class="pill">No password stored · OAuth only</span>
          </div>
        </div>
      </div>
      <div class="grid" style="margin-top:12px;">
        <div class="card">
          <strong>Safety</strong>
          <ul class="list">
            <li>Signed sessions, single-use forms</li>
            <li>Cooldown + IP throttling to block spam</li>
            <li>Discord interaction signatures verified</li>
          </ul>
        </div>
        <div class="card">
          <strong>Quality-of-life</strong>
          <ul class="list">
            <li>Auto-populated user info</li>
            <li>Clean, mobile-friendly layout</li>
            <li>Instant reference ID after submission</li>
          </ul>
        </div>
      </div>
      <div class="footer">Having trouble? Confirm you are logged into the correct Discord account before continuing.</div>
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
    ban = await fetch_ban_if_exists(user["id"])

    if not ban:
        content = f"""
          <div class="card status">
            <div class="stack">
              <div class="badge">No ban detected</div>
              <p class="subtitle">We could not find an active ban for <strong>{html.escape(user['username'])}#{html.escape(user.get('discriminator','0'))}</strong>.</p>
            </div>
          </div>
          <div class="actions">
            <a class="btn secondary" href="/">Return home</a>
          </div>
        """
        return HTMLResponse(render_page("No active ban", content), status_code=200, headers={"Cache-Control": "no-store"})

    session = serializer.dumps(
        {
            "uid": user["id"],
            "uname": f"{user['username']}#{user.get('discriminator','0')}",
            "ban_reason": ban.get("reason", "No reason provided."),
            "iat": time.time(),
        }
    )
    uname = html.escape(f"{user['username']}#{user.get('discriminator','0')}")
    ban_reason = html.escape(ban.get("reason", "No reason provided."))
    cooldown_minutes = max(1, APPEAL_COOLDOWN_SECONDS // 60)
    content = f"""
      <div class="header">
        <div class="brand">
          <div class="badge">Appeal form</div>
          <div class="pill">Ref #{session[:8]}</div>
        </div>
        <span class="muted">Cooldown: {cooldown_minutes} min between submissions</span>
      </div>
      <div class="card">
        <div class="stack">
          <div class="status">Submitting as <strong>{uname}</strong></div>
          <div class="status">Ban reason: {ban_reason}</div>
        </div>
      </div>
      <form class="card form" action="/submit" method="post">
        <input type="hidden" name="session" value="{session}" />
        <div class="field">
          <label for="evidence">Ban evidence (optional)</label>
          <input name="evidence" type="text" placeholder="Links or notes you have" />
        </div>
        <div class="field">
          <label for="appeal_reason">Why should you be unbanned?</label>
          <textarea name="appeal_reason" required placeholder="Explain what happened and what has changed."></textarea>
        </div>
        <div class="actions">
          <button class="btn" type="submit">Submit appeal</button>
          <a class="btn secondary" href="/">Cancel</a>
        </div>
        <div class="muted">Your submission is single-use and expires after {SESSION_TTL_SECONDS // 60} minutes.</div>
      </form>
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
    # prune old used sessions
    stale_sessions = [token for token, ts in _used_sessions.items() if now - ts > SESSION_TTL_SECONDS * 2]
    for token in stale_sessions:
        _used_sessions.pop(token, None)

    success = f"""
      <div class="card status">
        <div class="stack">
          <div class="badge">Appeal submitted</div>
          <p class="subtitle">Reference ID: <strong>{appeal_id}</strong></p>
          <p class="subtitle">We will review your appeal shortly. You will be notified in Discord.</p>
        </div>
      </div>
      <div class="actions">
        <a class="btn secondary" href="/">Back home</a>
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


async def edit_original(message: dict, content: Optional[str] = None, color: int = 0x2ecc71):
    embeds = message.get("embeds") or []
    if embeds:
        embeds[0]["color"] = color
        embeds[0]["fields"] = embeds[0].get("fields", []) + [
            {"name": "Decision", "value": content or "Updated", "inline": False}
        ]
    return {"type": 7, "data": {"embeds": embeds, "components": []}}


async def dm_user(user_id: str, embed: dict):
    async with httpx.AsyncClient() as client:
        dm = await client.post(
            f"{DISCORD_API_BASE}/users/@me/channels",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"recipient_id": user_id},
        )
        if dm.status_code not in (200, 201):
            return
        channel_id = dm.json().get("id")
        if not channel_id:
            return
        await client.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed]},
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
        roles = set(map(int, member.get("roles", [])))
        if MODERATOR_ROLE_ID not in roles:
            return await respond_ephemeral("You do not have permission to handle appeals.")

        try:
            action, appeal_id, user_id = custom_id.split(":")
        except ValueError:
            return await respond_ephemeral("Invalid interaction payload.")

        # Basic replay/spam guard: ignore if custom_id format looks wrong or missing ids
        if not appeal_id or not user_id:
            return await respond_ephemeral("Invalid interaction data.")

        # Run the heavy work in background to avoid interaction timeouts.
        async def handle_accept():
            async with httpx.AsyncClient() as client:
                await client.delete(
                    f"{DISCORD_API_BASE}/guilds/{TARGET_GUILD_ID}/bans/{user_id}",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                )
                await client.post(
                    f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={
                        "content": (
                            f"Appeal `{appeal_id}` accepted by <@{member.get('user', {}).get('id')}>. "
                            f"User <@{user_id}> unbanned."
                        )
                    },
                )
                # Delete the original appeal message
                await client.delete(
                    f"{DISCORD_API_BASE}/channels/{payload['channel_id']}/messages/{payload['message']['id']}",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                )
            await dm_user(
                user_id,
                {
                    "title": "Appeal Accepted",
                    "description": "Your appeal was accepted. You have been unbanned.",
                    "color": 0x2ECC71,
                },
            )

        async def handle_decline():
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{DISCORD_API_BASE}/channels/{APPEAL_LOG_CHANNEL_ID}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={
                        "content": (
                            f"Appeal `{appeal_id}` declined by <@{member.get('user', {}).get('id')}>. "
                            f"User <@{user_id}> notified."
                        )
                    },
                )
            await dm_user(
                user_id,
                {
                    "title": "Appeal Declined",
                    "description": "Your appeal was declined.",
                    "color": 0xE74C3C,
                },
            )

        if action == "web_appeal_accept":
            asyncio.create_task(handle_accept())
            return await respond_ephemeral("Processing appeal acceptance...")

        if action == "web_appeal_decline":
            asyncio.create_task(handle_decline())
            return await respond_ephemeral("Processing appeal decline...")

    return JSONResponse({"type": 4, "data": {"content": "Unsupported interaction", "flags": 1 << 6}})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
