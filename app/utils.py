import time
import html
import hashlib
import logging
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from itsdangerous import URLSafeSerializer, BadSignature
from fastapi import Request, Response
from config import SECRET_KEY, PERSIST_SESSION_SECONDS, SESSION_COOKIE_NAME

# Translation strings
LANG_STRINGS = {
    "en": {
        "hero_title": "Appeal your Discord ban.",
        "hero_sub": "Login to Discord to see details, review recent context, and submit an appeal.",
        "login": "Login with Discord",
        "how_it_works": "How it works",
        "step_1": "Authenticate with Discord to confirm it's your account.",
        "step_2": "Review ban details, share evidence, and submit securely.",
        "step_3": "Stay signed in to monitor your appeal status.",
        "appeal_cta": "Appeal your ban",
        "appeal_blurb": "Submit one appeal within the allowed window. We'll keep you signed in to track the decision.",
        "status_cta": "View status",
        "stay_signed_in": "Stay signed in",
        "stay_signed_in_blurb": "We keep your session secured so you can check decisions anytime.",
        "history_title": "Appeal history",
        "history_blurb": "",
        "welcome_back": "Welcome back",
        "review_ban": "Review my ban",
        "start_now": "Start now",
        "error_retry": "Retry",
        "error_home": "Go Home",
        "ban_details": "Ban details",
        "messages_header": "Recent messages",
        "no_messages": "No cached messages available.",
        "language_switch": "Switch language",
    },
    "es": {
        "hero_title": "Apela tu expulsión de Discord con confianza.",
        "hero_sub": "Verifica tu identidad, revisa por qué fuiste expulsado, mira el contexto reciente y envía una única apelación.",
        "login": "Iniciar sesión con Discord",
        "how_it_works": "Cómo funciona",
        "step_1": "Autentícate con Discord para confirmar que es tu cuenta.",
        "step_2": "Revisa los detalles del baneo, comparte evidencia y envía tu apelación de forma segura.",
        "step_3": "Mantente conectado para seguir el estado de tu apelación.",
        "appeal_cta": "Apelar tu expulsión",
        "appeal_blurb": "Envía una apelación dentro del periodo permitido. Mantendremos tu sesión para seguir la decisión.",
        "status_cta": "Ver estado",
        "stay_signed_in": "Mantente conectado",
        "stay_signed_in_blurb": "Guardamos tu sesión de forma segura para que revises decisiones en cualquier momento.",
        "history_title": "Historial de apelaciones",
        "history_blurb": "",
        "welcome_back": "Bienvenido de nuevo",
        "review_ban": "Revisar mi expulsión",
        "start_now": "Comenzar",
        "error_retry": "Reintentar",
        "error_home": "Ir al inicio",
        "ban_details": "Detalles del baneo",
        "messages_header": "Mensajes recientes (cacheados)",
        "no_messages": "No hay mensajes cacheados.",
        "language_switch": "Cambiar idioma",
    },
}

# In-memory cache for translations
LANG_CACHE: Dict[str, Dict[str, str]] = {}

# Serializer for session cookies
serializer = URLSafeSerializer(SECRET_KEY, salt="appeals-portal")

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

def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept

def uid(value: Any) -> str:
    return str(value)