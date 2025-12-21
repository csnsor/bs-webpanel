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
        "nav_how_it_works": "How it works",
        "nav_terms": "Terms",
        "nav_privacy": "Privacy",
        "nav_status": "Appeal Status",
        "nav_discord": "Discord",
        "brand_tag": "Ban Appeal Portal",
        "home_hero_title": "Resolve your ban the right way.",
        "home_section_title": "BlockSpin Appeals",
        "home_section_body": "Welcome to the official BlockSpin ban appeal portal. This site is used to submit and review appeals related to BlockSpin moderation actions. Appeals are handled under a single linked account to ensure accurate review and consistent history. Please read how the process works before submitting an appeal.",
        "home_status_cta": "View Appeal Status",
        "home_learn_more_cta": "Learn more",
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
        "hiw_intro_blurb": "Link either account, follow the clear appeal flow, and keep all moderators informed.",
        "hiw_step1_title": "Authenticate",
        "hiw_step1_body": "Start by signing in with Discord or Roblox. Each login seeds the internal user record.",
        "hiw_step2_title": "Link both accounts",
        "hiw_step2_body": "Connect your other platform from the header actions or live prompts so appeals merge seamlessly.",
        "hiw_step3_title": "Check status",
        "hiw_step3_body": "Use the Status page to review every appeal tied to your linked accounts, including moderator decisions and status updates.",
        "hiw_step4_title": "Submit respectfully",
        "hiw_step4_body": "Once both accounts are linked, choose the correct form, explain the context, and commit to improved behaviour.",
        "status_history_title_fmt": "Appeal history for {name}",
        "status_history_subtitle": "All linked appeals are shown in one timeline.",
        "status_back_home": "Back home",
    },
    "ar": {
        "hero_title": "حل حظر BlockSpin الخاص بك.",
        "hero_sub": "سجّل الدخول عبر ديسكورد للتأكد من هويتك، راجع سياق الحظر، وقدّم استئنافاً محترماً.",
        "login": "المتابعة باستخدام ديسكورد",
        "login_roblox": "المتابعة باستخدام روبلوكس",
        "how_it_works": "كيف يعمل",
        "status_cta": "تتبع الاستئناف",
        "history_title": "سجل الاستئناف",
        "review_ban": "مراجعة الحظر",
        "error_retry": "إعادة المحاولة",
        "error_home": "العودة للرئيسية",
        "ban_details": "تفاصيل الحظر",
        "messages_header": "السياق الأخير",
        "no_messages": "لا توجد رسائل حديثة.",
        "language_switch": "تغيير اللغة",
        "link_discord_prompt": "اربط حساب ديسكورد لتلقي التحديثات حول هذا الاستئناف.",
        "link_discord_cta": "ربط ديسكورد",
        "link_roblox_prompt": "اربط حساب روبلوكس لمزامنة سجل الاستئناف.",
        "link_roblox_cta": "ربط روبلوكس",
    },
    "th": {
        "hero_title": "แก้ไขการแบน BlockSpin ของคุณ",
        "hero_sub": "เข้าสู่ระบบด้วย Discord เพื่อยืนยันตัวตน ตรวจสอบสาเหตุการแบน และส่งคำอุทธรณ์อย่างสุภาพ",
        "login": "เข้าสู่ระบบด้วย Discord",
        "login_roblox": "เข้าสู่ระบบด้วย Roblox",
        "how_it_works": "วิธีการทำงาน",
        "status_cta": "ติดตามคำอุทธรณ์",
        "history_title": "ประวัติคำอุทธรณ์",
        "review_ban": "ตรวจสอบการแบน",
        "error_retry": "ลองอีกครั้ง",
        "error_home": "กลับหน้าหลัก",
        "ban_details": "รายละเอียดการแบน",
        "messages_header": "บริบทล่าสุด",
        "no_messages": "ไม่มีข้อความล่าสุด",
        "language_switch": "เปลี่ยนภาษา",
        "link_discord_prompt": "เชื่อมต่อ Discord เพื่อรับการอัปเดตเกี่ยวกับคำอุทธรณ์นี้",
        "link_discord_cta": "เชื่อมต่อ Discord",
        "link_roblox_prompt": "เชื่อมต่อ Roblox เพื่อซิงก์ประวัติคำอุทธรณ์ของคุณ",
        "link_roblox_cta": "เชื่อมต่อ Roblox",
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
    if lang in LANG_CACHE:
        return LANG_CACHE[lang]
    if lang == "en":
        return base

    # If we have a partial manual translation, merge and fill gaps automatically.
    manual = LANG_STRINGS.get(lang)
    merged: Dict[str, str] = dict(base)
    if manual:
        merged.update(manual)
        missing_keys = [key for key in base.keys() if key not in manual]
    else:
        missing_keys = list(base.keys())

    for key in missing_keys:
        try:
            merged[key] = await translate_text(base[key], target_lang=lang, source_lang="en")
        except Exception:
            merged[key] = merged.get(key, base[key])

    LANG_CACHE[lang] = merged
    return merged


async def detect_language(request: Request, lang_param: Optional[str] = None) -> str:
    if lang_param:
        return normalize_language(lang_param)
    cookie_lang = request.cookies.get("lang")
    if cookie_lang:
        return normalize_language(cookie_lang)

    accept = request.headers.get("accept-language", "")
    accept_lang = normalize_language(accept.split(",")[0].strip()) if accept else None
    ip_lang: Optional[str] = None

    ip = get_client_ip(request)
    if ip and ip not in {"127.0.0.1", "::1", "unknown"}:
        try:
            client = get_http_client()
            resp = await client.get(f"https://ipapi.co/{ip}/json/", timeout=3)
            if resp.status_code == 200:
                data = resp.json() or {}
                cc_lang_map = {
                    # Arabic-speaking countries
                    "sa": "ar",
                    "ae": "ar",
                    "eg": "ar",
                    "om": "ar",
                    "qa": "ar",
                    "bh": "ar",
                    "kw": "ar",
                    "ma": "ar",
                    "dz": "ar",
                    "tn": "ar",
                    "jo": "ar",
                    "iq": "ar",
                    "ye": "ar",
                    "ly": "ar",
                    "ps": "ar",
                    "lb": "ar",
                    "sy": "ar",
                    "sd": "ar",
                    # Spanish-speaking
                    "es": "es",
                    "mx": "es",
                    "ar": "es",
                    "cl": "es",
                    "co": "es",
                    "pe": "es",
                    "pr": "es",
                    "uy": "es",
                    "py": "es",
                    "bo": "es",
                    "do": "es",
                    "gt": "es",
                    "sv": "es",
                    "hn": "es",
                    "ni": "es",
                    "cr": "es",
                    "pa": "es",
                    "ve": "es",
                    "ec": "es",
                    # Thai
                    "th": "th",
                }
                langs = data.get("languages")
                if langs:
                    lang_candidate = normalize_language(langs.split(",")[0])
                    mapped = cc_lang_map.get(lang_candidate)
                    ip_lang = mapped or lang_candidate
                cc = data.get("country_code")
                if cc:
                    cc_norm = cc.lower()
                    mapped = cc_lang_map.get(cc_norm)
                    ip_lang = mapped or normalize_language(cc_norm)
        except Exception as exc:
            logging.warning("Geo lookup failed for ip=%s error=%s", ip, exc)
    if ip_lang:
        return normalize_language(ip_lang)
    if accept_lang:
        return normalize_language(accept_lang)
    return "en"
