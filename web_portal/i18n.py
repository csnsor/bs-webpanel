from __future__ import annotations

import logging
from typing import Dict, Optional

from fastapi import Request

from .clients import get_http_client
from .settings import LIBRETRANSLATE_URL
from .utils import get_client_ip, normalize_language


LANG_STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "hero_title": "Resolve your BlockSpin ban.",
        "hero_sub": "Sign in with Discord to confirm your identity, review ban context, and submit a respectful appeal.",
        "login": "Continue with Discord",
        "login_roblox": "Continue with Roblox",
        "how_it_works": "How it works",
        "status_cta": "Track my appeal",
        "history_title": "Appeal history",
        "review_ban": "Review my ban",
        "error_retry": "Retry",
        "error_home": "Go home",
        "ban_details": "Ban details",
        "messages_header": "Recent context",
        "no_messages": "No recent messages available.",
        "language_switch": "Switch language",
        "link_discord_prompt": "Connect your Discord to receive updates about this appeal.",
        "link_discord_cta": "Connect Discord",
        "link_roblox_prompt": "Connect your Roblox account to sync appeal history.",
        "link_roblox_cta": "Connect Roblox",
    },
    "es": {
        "hero_title": "Resuelve tu baneo en BlockSpin.",
        "hero_sub": "Conecta con Discord, revisa el contexto y envía una apelación clara.",
        "login": "Continuar con Discord",
        "login_roblox": "Continuar con Roblox",
        "how_it_works": "Como funciona",
        "status_cta": "Ver mi apelacion",
        "history_title": "Historial de apelaciones",
        "review_ban": "Revisar mi baneo",
        "error_retry": "Reintentar",
        "error_home": "Ir al inicio",
        "ban_details": "Detalles del baneo",
        "messages_header": "Contexto reciente",
        "no_messages": "No hay mensajes recientes.",
        "language_switch": "Cambiar idioma",
        "link_discord_prompt": "Conecta tu Discord para recibir actualizaciones sobre esta apelación.",
        "link_discord_cta": "Conectar Discord",
        "link_roblox_prompt": "Conecta tu cuenta de Roblox para sincronizar tu historial de apelaciones.",
        "link_roblox_cta": "Conectar Roblox",
    },
}

LANG_CACHE: Dict[str, Dict[str, str]] = {}


async def translate_text(text: str, target_lang: str = "en", source_lang: Optional[str] = None) -> str:
    if not text or (normalize_language(target_lang) == "en" and normalize_language(source_lang) == "en"):
        return text
    # Try primary provider
    providers = [
        ("primary", LIBRETRANSLATE_URL),
        ("gtx", "https://translate.googleapis.com/translate_a/single"),
        ("mymemory", "https://api.mymemory.translated.net/get"),
    ]
    for name, url in providers:
        try:
            client = get_http_client()
            if name == "gtx":
                params = {
                    "client": "gtx",
                    "sl": source_lang or "auto",
                    "tl": target_lang,
                    "dt": "t",
                    "q": text,
                }
                resp = await client.get(url, params=params, timeout=8)
                if resp.status_code == 200:
                    data = resp.json()
                    # Google translate API style response: [[["translated","original",...]],...]
                    if data and isinstance(data, list) and data[0] and data[0][0]:
                        return data[0][0][0] or text
            elif name == "mymemory":
                params = {
                    "q": text,
                    "langpair": f"{source_lang or 'auto'}|{target_lang}",
                }
                resp = await client.get(url, params=params, timeout=8)
                if resp.status_code == 200:
                    data = resp.json() or {}
                    translated = (data.get("responseData") or {}).get("translatedText")
                    if translated:
                        return translated
            else:
                resp = await client.post(
                    url,
                    json={
                        "q": text,
                        "source": source_lang or "auto",
                        "target": target_lang,
                        "format": "text",
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=8,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("translatedText") or text
            logging.warning("Translation failed provider=%s status=%s body=%s", name, resp.status_code, resp.text)
        except Exception as exc:
            logging.warning("Translation exception provider=%s error=%s", name, exc)
    return text


async def get_strings(lang: str) -> Dict[str, str]:
    lang = normalize_language(lang)
    base = LANG_STRINGS["en"]
    if lang in LANG_STRINGS:
        return LANG_STRINGS[lang]
    if lang in LANG_CACHE:
        return LANG_CACHE[lang]
    translated: Dict[str, str] = {}
    for key, text in base.items():
        translated[key] = await translate_text(text, target_lang=lang, source_lang="en")
    merged = {**base, **translated}
    LANG_CACHE[lang] = merged
    return merged


async def detect_language(request: Request, lang_param: Optional[str] = None) -> str:
    if lang_param:
        return normalize_language(lang_param)
    cookie_lang = request.cookies.get("lang")
    if cookie_lang:
        return normalize_language(cookie_lang)
    accept = request.headers.get("accept-language", "")
    if accept:
        return normalize_language(accept.split(",")[0].strip())
    ip = get_client_ip(request)
    if ip and ip not in {"127.0.0.1", "::1", "unknown"}:
        try:
            client = get_http_client()
            resp = await client.get(f"https://ipapi.co/{ip}/json/", timeout=3)
            if resp.status_code == 200:
                data = resp.json() or {}
                langs = data.get("languages")
                if langs:
                    return normalize_language(langs.split(",")[0])
                cc = data.get("country_code")
                if cc:
                    return normalize_language(cc.lower())
        except Exception as exc:
            logging.warning("Geo lookup failed for ip=%s error=%s", ip, exc)
    return "en"
