import os
import secrets

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
TARGET_GUILD_NAME = os.getenv("TARGET_GUILD_NAME")
MODERATOR_ROLE_ID_1 = int(os.getenv("MODERATOR_ROLE_ID_1", "1383147193602408642"))
MODERATOR_ROLE_ID_2 = int(os.getenv("MODERATOR_ROLE_ID_2", "1378115294731440328"))
MODERATOR_ROLE_IDS = {MODERATOR_ROLE_ID_1, MODERATOR_ROLE_ID_2}
APPEAL_CHANNEL_ID = int(os.getenv("APPEAL_CHANNEL_ID", "1449872679698960517"))
ROBLOX_UNBAN_REQUEST_CHANNEL_ID = int(os.getenv("ROBLOX_UNBAN_REQUEST_CHANNEL_ID", "1345697823768707072"))
APPEAL_LOG_CHANNEL_ID = int(os.getenv("APPEAL_LOG_CHANNEL_ID", "1353445286457901106"))
AUTH_LOG_CHANNEL_ID = int(os.getenv("AUTH_LOG_CHANNEL_ID", "1449822248490762421"))
SECRET_KEY = os.getenv("PORTAL_SECRET_KEY") or secrets.token_hex(16)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "discord-appeals"
SUPABASE_SESSION_TABLE = "discord-appeal-sessions"
SUPABASE_CONTEXT_TABLE = "banned_user_context"

# Roblox settings
ROBLOX_CLIENT_ID = os.getenv("ROBLOX_CLIENT_ID")
ROBLOX_CLIENT_SECRET = os.getenv("ROBLOX_CLIENT_SECRET")
ROBLOX_REDIRECT_URI = os.getenv("ROBLOX_REDIRECT_URI", "https://bs-appeals.up.railway.app/oauth/roblox/callback")
ROBLOX_BAN_API_KEY = os.getenv("ROBLOX_BAN_API_KEY")
ROBLOX_BAN_API_URL = os.getenv("ROBLOX_BAN_API_URL", "https://apis.roblox.com/cloud/v2/universes/6765805766/user-restrictions")  # The base URL for the ban/restriction API
ROBLOX_APPEAL_CHANNEL_ID = int(os.getenv("ROBLOX_APPEAL_CHANNEL_ID", "1352973388334764112"))
ROBLOX_SUPABASE_TABLE = "roblox-appeals"


INVITE_LINK = "https://discord.gg/blockspin"
MESSAGE_CACHE_GUILD_ID_DEFAULT = "1337420081382297682"
MESSAGE_CACHE_GUILD_ID_RAW = (os.getenv("MESSAGE_CACHE_GUILD_ID") or MESSAGE_CACHE_GUILD_ID_DEFAULT).split(",")[0].strip()
MESSAGE_CACHE_GUILD_ID = int(MESSAGE_CACHE_GUILD_ID_RAW or MESSAGE_CACHE_GUILD_ID_DEFAULT)
# Accept/Decline should re-add users to the single BlockSpin guild.
READD_GUILD_ID = str(MESSAGE_CACHE_GUILD_ID)
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate")
DEBUG_EVENTS = os.getenv("DEBUG_EVENTS", "false").lower() == "true"
# Bot logging defaults to enabled so deployments have visibility into caching/ban events.
BOT_EVENT_LOGGING = os.getenv("BOT_EVENT_LOGGING", "true").lower() in {"1", "true", "yes", "on"}
# Message bodies can contain private data; keep disabled unless explicitly enabled (or DEBUG_EVENTS).
BOT_MESSAGE_LOG_CONTENT = os.getenv("BOT_MESSAGE_LOG_CONTENT", "false").lower() in {"1", "true", "yes", "on"} or DEBUG_EVENTS
# By default, do not persist rolling message snapshots to Supabase. We keep the last 15 per-user in RAM and only write
# to Supabase when a ban is detected (banned_user_context).
ENABLE_MESSAGE_SNAPSHOTS = os.getenv("ENABLE_MESSAGE_SNAPSHOTS", "false").lower() in {"1", "true", "yes", "on"}

OAUTH_SCOPES = "identify guilds.join"
ROBLOX_OAUTH_SCOPES = "openid profile"
DISCORD_API_BASE = "https://discord.com/api/v10"
ROBLOX_API_BASE = "https://apis.roblox.com"


# Basic portal settings
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
STATUS_DATA_CACHE_TTL_SECONDS = int(os.getenv("STATUS_DATA_CACHE_TTL_SECONDS", "5"))
GUILD_NAME_CACHE_TTL_SECONDS = int(os.getenv("GUILD_NAME_CACHE_TTL_SECONDS", "3600"))
RECENT_MESSAGE_CACHE_TTL = int(os.getenv("RECENT_MESSAGE_CACHE_TTL", "3600"))


def validate_required_envs() -> None:
    missing = [
        name
        for name, val in {
            "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
            "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
            "DISCORD_REDIRECT_URI": DISCORD_REDIRECT_URI,
            "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
            "DISCORD_PUBLIC_KEY": DISCORD_PUBLIC_KEY,
            "ROBLOX_CLIENT_ID": ROBLOX_CLIENT_ID,
            "ROBLOX_CLIENT_SECRET": ROBLOX_CLIENT_SECRET,
            "ROBLOX_REDIRECT_URI": ROBLOX_REDIRECT_URI,
            "ROBLOX_BAN_API_KEY": ROBLOX_BAN_API_KEY,
        }.items()
        if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

