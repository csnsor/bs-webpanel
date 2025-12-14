from typing import Optional, Dict, List, Any
from datetime import datetime
from pydantic import BaseModel

class DiscordUser(BaseModel):
    id: str
    username: str
    discriminator: str
    avatar: Optional[str] = None
    global_name: Optional[str] = None

class AppealSubmission(BaseModel):
    user_id: str
    ban_reason: str
    ban_evidence: str
    appeal_reason: str
    appeal_reason_original: str
    user_lang: str
    message_cache: Optional[List[dict]] = None
    ip: str
    forwarded_for: str
    user_agent: str

class AppealRecord(BaseModel):
    appeal_id: str
    user_id: str
    username: Optional[str] = None
    guild_id: str
    ban_reason: str
    ban_evidence: str
    appeal_reason: str
    appeal_reason_original: str
    user_lang: str
    status: str
    ip: str
    forwarded_for: str
    user_agent: str
    message_cache: Optional[List[dict]] = None
    created_at: Optional[datetime] = None
    decision_by: Optional[str] = None
    decision_at: Optional[int] = None
    dm_delivered: bool = False
    notes: Optional[str] = None

class MessageContext(BaseModel):
    user_id: str
    messages: List[dict]
    banned_at: int

class UserSession(BaseModel):
    uid: str
    uname: str
    display_name: Optional[str] = None
    iat: float