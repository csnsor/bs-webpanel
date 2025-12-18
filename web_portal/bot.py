from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .services.message_cache import _get_recent_message_context, maybe_snapshot_messages, should_track_messages, truncate_log_text
from .services.supabase import is_supabase_ready, supabase_request
from .settings import (
    BOT_EVENT_LOGGING,
    BOT_MESSAGE_LOG_CONTENT,
    DEBUG_EVENTS,
    DISCORD_BOT_TOKEN,
    MESSAGE_CACHE_GUILD_ID,
    SUPABASE_CONTEXT_TABLE,
)
from .state import _message_buffer, _recent_message_context
from .utils import uid

try:
    import discord  # type: ignore
except ImportError:  # allow app to boot even if discord.py isn't installed
    discord = None

bot_client = None


async def run_bot_forever() -> None:
    if not bot_client:
        raise RuntimeError("discord.py not available; bot client not started.")
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN missing; bot client cannot start.")
    backoff = 2.0
    while True:
        try:
            logging.info("Starting Discord bot gateway connection...")
            logging.info(
                "Bot logging enabled=%s message_content_logging=%s cache_guild_allowlist=%s",
                BOT_EVENT_LOGGING,
                BOT_MESSAGE_LOG_CONTENT,
                MESSAGE_CACHE_GUILD_ID,
            )
            if BOT_EVENT_LOGGING:
                logging.info("If message caching logs are missing, confirm the bot is online and has channel access + intents.")
            await bot_client.start(DISCORD_BOT_TOKEN)
            backoff = 2.0
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.exception("Discord bot task crashed: %s", exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, 60.0)


async def heartbeat() -> None:
    while True:
        try:
            ready = bool(bot_client and getattr(bot_client, "is_ready", lambda: False)())
            latency_ms = None
            try:
                if bot_client and getattr(bot_client, "latency", None) is not None:
                    latency_ms = int(float(bot_client.latency) * 1000)
            except Exception:
                latency_ms = None
            logging.info(
                "[bot] ready=%s latency_ms=%s cache_guild=%s buffer_users=%s",
                ready,
                latency_ms,
                MESSAGE_CACHE_GUILD_ID,
                len(_message_buffer),
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.debug("Bot heartbeat failed: %s", exc)
        await asyncio.sleep(60)


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
        try:
            await bot_client.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="Appeals on the BlockSpin Portal",
                ),
            )
        except Exception as exc:
            logging.debug("Failed to set presence: %s", exc)

    @bot_client.event
    async def on_disconnect():
        logging.warning("Discord bot disconnected from gateway.")

    @bot_client.event
    async def on_resumed():
        logging.info("Discord bot resumed gateway session.")

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
            user_label = (
                getattr(message.author, "global_name", None)
                or getattr(message.author, "display_name", None)
                or getattr(message.author, "name", "unknown")
            )
            channel_name = getattr(message.channel, "name", "unknown")
            log_content = truncate_log_text(content) if BOT_MESSAGE_LOG_CONTENT else f"<len={len(content)}>"
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
        await maybe_snapshot_messages(user_id, str(message.guild.id))

        # Administrator-only system health command
        content = (message.content or "").strip().lower()
        if content.startswith("!appeal_health"):
            if not getattr(message.author.guild_permissions, "administrator", False):
                return

            start = time.perf_counter()

            # Discord gateway status
            bot_ready = bool(bot_client and getattr(bot_client, "is_ready", lambda: False)())
            latency_ms = None
            try:
                if bot_client and bot_client.latency is not None:
                    latency_ms = round(float(bot_client.latency) * 1000)
            except Exception:
                latency_ms = None

            # Database status
            supabase_ok = is_supabase_ready()

            # Background worker state
            try:
                if _bot_task is None:
                    worker_state = "not started"
                elif _bot_task.cancelled():
                    worker_state = "cancelled"
                elif _bot_task.done():
                    worker_state = "stopped"
                else:
                    worker_state = "running"
            except Exception:
                worker_state = "unknown"

            elapsed_ms = round((time.perf_counter() - start) * 1000)

            # Overall health color
            if bot_ready and supabase_ok:
                color = 0x2ECC71  # healthy
            elif bot_ready or supabase_ok:
                color = 0xE67E22  # degraded
            else:
                color = 0xE74C3C  # unhealthy

            latency_display = f"{latency_ms} ms" if latency_ms is not None else "n/a"

            description = (
                f"**Discord Gateway:** {'Online' if bot_ready else 'Offline'} ({latency_display})\n"
                f"**Database:** {'Ready' if supabase_ok else 'Unavailable'}\n"
                f"**Worker State:** {worker_state}\n"
                f"**Response Time:** {elapsed_ms} ms"
            )

            try:
                embed = discord.Embed(
                    title="Appeals System Health",
                    description=description,
                    color=color,
                    timestamp=datetime.now(timezone.utc),
                )
                await message.channel.send(embed=embed)
            except Exception as exc:
                logging.warning("Failed to send system health embed: %s", exc)

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
                prefer="resolution=merge-duplicates,return=minimal",
            )
            if BOT_EVENT_LOGGING:
                logging.info(
                    "[ban_supabase] user=%s guild=%s ok=%s returned_rows=%s",
                    user_id,
                    guild.id,
                    bool(result),
                    (len(result) if isinstance(result, list) else (1 if isinstance(result, dict) else 0)),
                )

        _message_buffer.pop(user_id, None)
        _recent_message_context.pop(user_id, None)
