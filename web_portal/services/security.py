from __future__ import annotations

import secrets
import time

from fastapi import HTTPException

from ..settings import APPEAL_IP_MAX_REQUESTS, APPEAL_IP_WINDOW_SECONDS
from ..state import _ip_requests, _state_tokens


def issue_state_token(ip: str) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    _state_tokens[token] = (ip, now)
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


def enforce_ip_rate_limit(ip: str) -> None:
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

