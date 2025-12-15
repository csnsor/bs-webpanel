from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Request

from .settings import SECRET_KEY


def uid(value: Any) -> str:
    return str(value)


def normalize_language(lang: Optional[str]) -> str:
    if not lang:
        return "en"
    lang = lang.split(",")[0].split(";")[0].strip().lower()
    if "-" in lang:
        lang = lang.split("-")[0]
    return lang or "en"


def format_timestamp(value: Any) -> str:
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


def format_relative(seconds: float) -> str:
    try:
        seconds = float(seconds)
    except Exception:
        return ""
    seconds = max(0.0, seconds)
    total = int(seconds)
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h ago"
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{max(0, minutes)}m ago"


def simplify_ban_reason(reason: Optional[str]) -> str:
    if not reason:
        return ""
    reason = str(reason).strip()
    if not reason:
        return ""
    if ":" in reason:
        tail = reason.rsplit(":", 1)[-1].strip()
        return tail or reason
    return reason


def hash_value(raw: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{raw}".encode("utf-8", "ignore")).hexdigest()


def hash_ip(ip: str) -> str:
    if not ip or ip == "unknown":
        return "unknown"
    return hash_value(ip)


def clean_display_name(raw: str) -> str:
    if not raw:
        return ""
    if raw.endswith("#0"):
        return raw[:-2]
    return raw


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept

