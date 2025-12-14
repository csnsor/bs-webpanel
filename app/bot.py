import logging
import time
import asyncio
from typing import Dict, List, Optional
from collections import deque, defaultdict
import discord
from config import (
    DISCORD_BOT_TOKEN, TARGET_GUILD_ID, DM_GUILD_ID, 
    REMOVE_FROM_DM_GUILD_AFTER_DM, CLEANUP_DM_INVITES,
    MESSAGE_CACHE_GUILD_IDS_RAW, DEBUG_EVENTS
)
from app.utils import uid
from app.database import persist_message_snapshot

# In-memory stores for message caching
_message_buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=15))
_recent_message_context: Dict[str, Tuple[List[dict], float]] = {}

# Determine which guilds to track messages for
MESSAGE_CACHE_GUILD_IDS = {
    gid.strip()
    for gid in MESSAGE_CACHE_GUILD_IDS_RAW.split(",")
    if gid.strip()
}
if not MESSAGE_CACHE_GUILD_IDS:
    MESSAGE_CACHE_GUILD_IDS = None

def should_track_messages(guild_id: int) -> bool:
    if MESSAGE_CACHE_GUILD_IDS is None:
        return True
    return str(guild_id) in MESSAGE_CACHE_GUILD_IDS

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

async def maybe_snapshot_messages(user_id: str, guild_id: str):
    if not is_supabase_ready():
        return
    if not should_track_messages(guild_id):
        logging.debug("Message caching skipped for guild %s", guild_id)
        return
    entries = list(_message_buffer.get(user_id, []))
    if not entries:
        return
    await persist_message_snapshot(user_id, entries[-15:])

# Discord bot setup
try:
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

        if cached_msgs and is_supabase_ready():
            try:
                await supabase_request(
                    "post",
                    "banned_user_context",
                    params={"on_conflict": "user_id"},
                    payload={
                        "user_id": user_id,
                        "messages": cached_msgs,
                        "banned_at": int(time.time()),
                    },
                    prefer="resolution=merge-duplicates",
                )
                logging.info("Saved %d messages to Supabase for %s", len(cached_msgs), user_id)
            except Exception as exc:
                logging.warning("Failed to store banned context for %s: %s", user_id, exc)

        _message_buffer.pop(user_id, None)
        _recent_message_context.pop(user_id, None)

except ImportError:
    bot_client = None
    logging.warning("discord.py not available; bot client not started.")

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