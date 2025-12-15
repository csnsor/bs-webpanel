from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

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
_status_data_cache: Dict[str, Tuple[dict, float]] = {}  # {user_id: (payload, ts)}
_guild_name_cache: Dict[str, Tuple[str, float]] = {}  # {guild_id: (name, ts)}

# Bot & message cache
_bot_task: Optional[asyncio.Task] = None
_bot_heartbeat_task: Optional[asyncio.Task] = None
_message_buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=15))
_recent_message_context: Dict[str, Tuple[List[dict], float]] = {}

