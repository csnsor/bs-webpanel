from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
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
        "nav_how_it_works": "كيف يعمل",
        "nav_terms": "الشروط",
        "nav_privacy": "الخصوصية",
        "nav_status": "حالة الاستئناف",
        "nav_discord": "ديسكورد",
        "brand_tag": "بوابة الاستئناف من الحظر",
        "home_hero_title": "حل حظرك بالطريقة الصحيحة.",
        "home_section_title": "استئنافات BlockSpin",
        "home_section_body": "مرحباً بك في بوابة الاستئناف الرسمية من حظر BlockSpin. يُستخدم هذا الموقع لتقديم ومراجعة الاستئنافات المتعلقة بإجراءات الإشراف في BlockSpin. تتم معالجة الاستئنافات تحت حساب مرتبط واحد لضمان المراجعة الدقيقة والتاريخ المتسق. يرجى قراءة كيفية عمل العملية قبل تقديم استئناف.",
        "home_status_cta": "عرض حالة الاستئناف",
        "home_learn_more_cta": "اعرف المزيد",
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
        "hiw_intro_blurb": "اربط أي حساب، اتبع تدفق الاستئناف الواضح، وابقِ جميع المشرفين على اطلاع.",
        "hiw_step1_title": "المصادقة",
        "hiw_step1_body": "ابدأ بتسجيل الدخول باستخدام ديسكورد أو روبلوكس. كل تسجيل دخول يبدأ سجل المستخدم الداخلي.",
        "hiw_step2_title": "ربط كلا الحسابين",
        "hiw_step2_body": "قم بتوصيل منصتك الأخرى من إجراءات الرأس أو المطالبات المباشرة حتى تندمج الاستئنافات بسلاسة.",
        "hiw_step3_title": "تحقق من الحالة",
        "hiw_step3_body": "استخدم صفحة الحالة لمراجعة كل استئناف مرتبط بحساباتك المرتبطة، بما في ذلك قرارات المشرفين وتحديثات الحالة.",
        "hiw_step4_title": "قدم باحترام",
        "hiw_step4_body": "بمجرد ربط كلا الحسابين، اختر النموذج الصحيح، اشرح السياق، والتزم بتحسين السلوك.",
        "status_history_title_fmt": "سجل الاستئناف لـ {name}",
        "status_history_subtitle": "جميع الاستئنافات المرتبطة معروضة في خط زمني واحد.",
        "status_back_home": "العودة للرئيسية",
    },
    "th": {
        "hero_title": "แก้ไขการแบน BlockSpin ของคุณ",
        "hero_sub": "เข้าสู่ระบบด้วย Discord เพื่อยืนยันตัวตน ตรวจสอบสาเหตุการแบน และส่งคำอุทธรณ์อย่างสุภาพ",
        "login": "เข้าสู่ระบบด้วย Discord",
        "login_roblox": "เข้าสู่ระบบด้วย Roblox",
        "how_it_works": "วิธีการทำงาน",
        "nav_how_it_works": "วิธีการทำงาน",
        "nav_terms": "เงื่อนไข",
        "nav_privacy": "ความเป็นส่วนตัว",
        "nav_status": "สถานะการอุทธรณ์",
        "nav_discord": "Discord",
        "brand_tag": "พอร์ทัลการอุทธรณ์การแบน",
        "home_hero_title": "แก้ไขการแบนของคุณอย่างถูกต้อง",
        "home_section_title": "การอุทธรณ์ BlockSpin",
        "home_section_body": "ยินดีต้อนรับสู่พอร์ทัลการอุทธรณ์การแบนอย่างเป็นทางการของ BlockSpin ไซต์นี้ใช้สำหรับส่งและตรวจสอบการอุทธรณ์ที่เกี่ยวข้องกับการดำเนินการดูแลระบบของ BlockSpin การอุทธรณ์จะได้รับการดำเนินการภายใต้บัญชีที่เชื่อมโยงเดียวเพื่อให้แน่ใจว่าได้รับการตรวจสอบอย่างถูกต้องและมีประวัติที่สอดคล้องกัน กรุณาอ่านวิธีการทำงานก่อนส่งการอุทธรณ์",
        "home_status_cta": "ดูสถานะการอุทธรณ์",
        "home_learn_more_cta": "เรียนรู้เพิ่มเติม",
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
        "hiw_intro_blurb": "เชื่อมโยงบัญชีใดบัญชีหนึ่ง ทำตามขั้นตอนการอุทธรณ์ที่ชัดเจน และให้ผู้ดูแลทราบทุกอย่าง",
        "hiw_step1_title": "ยืนยันตัวตน",
        "hiw_step1_body": "เริ่มต้นด้วยการเข้าสู่ระบบด้วย Discord หรือ Roblox การเข้าสู่ระบบแต่ละครั้งจะสร้างบันทึกผู้ใช้ภายใน",
        "hiw_step2_title": "เชื่อมโยงทั้งสองบัญชี",
        "hiw_step2_body": "เชื่อมต่อแพลตฟอร์มอื่นของคุณจากการดำเนินการในส่วนหัวหรือพร้อมต์สดเพื่อให้การอุทธรณ์ผสานเข้าด้วยกันอย่างราบรื่น",
        "hiw_step3_title": "ตรวจสอบสถานะ",
        "hiw_step3_body": "ใช้หน้าสถานะเพื่อตรวจสอบการอุทธรณ์ทั้งหมดที่เชื่อมโยงกับบัญชีของคุณ รวมถึงการตัดสินใจของผู้ดูแลและการอัปเดตสถานะ",
        "hiw_step4_title": "ส่งอย่างสุภาพ",
        "hiw_step4_body": "เมื่อเชื่อมโยงทั้งสองบัญชีแล้ว เลือกแบบฟอร์มที่ถูกต้อง อธิบายบริบท และมุ่งมั่นที่จะปรับปรุงพฤติกรรม",
        "status_history_title_fmt": "ประวัติการอุทธรณ์สำหรับ {name}",
        "status_history_subtitle": "การอุทธรณ์ที่เชื่อมโยงทั้งหมดแสดงในไทม์ไลน์เดียว",
        "status_back_home": "กลับหน้าหลัก",
    },
    "es": {
        "hero_title": "Resuelve tu baneo en BlockSpin.",
        "hero_sub": "Conecta con Discord, revisa el contexto y envía una apelación clara.",
        "login": "Continuar con Discord",
        "login_roblox": "Continuar con Roblox",
        "how_it_works": "Como funciona",
        "nav_how_it_works": "Como funciona",
        "nav_terms": "Términos",
        "nav_privacy": "Privacidad",
        "nav_status": "Estado de la apelación",
        "nav_discord": "Discord",
        "brand_tag": "Portal de apelaciones de baneos",
        "home_hero_title": "Resuelve tu baneo de la manera correcta.",
        "home_section_title": "Apelaciones de BlockSpin",
        "home_section_body": "Bienvenido al portal oficial de apelaciones de baneos de BlockSpin. Este sitio se utiliza para enviar y revisar apelaciones relacionadas con acciones de moderación de BlockSpin. Las apelaciones se manejan bajo una única cuenta vinculada para garantizar una revisión precisa y un historial consistente. Por favor, lee cómo funciona el proceso antes de enviar una apelación.",
        "home_status_cta": "Ver estado de la apelación",
        "home_learn_more_cta": "Aprende más",
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
        "hiw_intro_blurb": "Vincula cualquiera de las cuentas, sigue el flujo de apelación claro y mantén informados a todos los moderadores.",
        "hiw_step1_title": "Autenticar",
        "hiw_step1_body": "Comienza iniciando sesión con Discord o Roblox. Cada inicio de sesión inicializa el registro de usuario interno.",
        "hiw_step2_title": "Vincular ambas cuentas",
        "hiw_step2_body": "Conecta tu otra plataforma desde las acciones del encabezado o las indicaciones en vivo para que las apelaciones se fusionen sin problemas.",
        "hiw_step3_title": "Verificar estado",
        "hiw_step3_body": "Usa la página de Estado para revisar cada apelación vinculada a tus cuentas conectadas, incluyendo decisiones de moderadores y actualizaciones de estado.",
        "hiw_step4_title": "Enviar respetuosamente",
        "hiw_step4_body": "Una vez que ambas cuentas estén vinculadas, elige el formulario correcto, explica el contexto y comprométete a mejorar tu comportamiento.",
        "status_history_title_fmt": "Historial de apelaciones para {name}",
        "status_history_subtitle": "Todas las apelaciones vinculadas se muestran en una sola línea de tiempo.",
        "status_back_home": "Volver al inicio",
    },
}

LANG_CACHE: Dict[str, Dict[str, str]] = {}
# Per-text translation cache to avoid repeated network calls for the same phrase.
TRANSLATION_CACHE: Dict[tuple[str, str, Optional[str]], str] = {}
_LANG_CACHE_FILE = Path(__file__).resolve().parent / "lang_cache.json"
_LANG_CACHE_LOCK = asyncio.Lock()

# Language display metadata for UI selectors.
LANG_META: Dict[str, Dict[str, str]] = {
    # Use explicit code points so Windows terminals don't mangle the emoji.
    "en": {"name": "English", "flag": "\U0001F1FA\U0001F1F8"},  # US
    "es": {"name": "Español", "flag": "\U0001F1EA\U0001F1F8"},  # Spain
    "ar": {"name": "العربية", "flag": "\U0001F1F8\U0001F1E6"},  # Saudi Arabia
    "th": {"name": "ไทย", "flag": "\U0001F1F9\U0001F1ED"},     # Thailand
}


def _load_lang_cache_from_disk() -> None:
    """Warm the in-memory language cache from disk so we don't re-translate on restart."""
    try:
        if not _LANG_CACHE_FILE.exists():
            return
        data = json.loads(_LANG_CACHE_FILE.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            for code, strings in data.items():
                if isinstance(strings, dict):
                    LANG_CACHE[normalize_language(code)] = strings
    except Exception as exc:
        logging.warning("Failed to load language cache: %s", exc)


async def _persist_lang_cache() -> None:
    """Persist merged language strings to disk so future visitors skip translation API calls."""
    try:
        async with _LANG_CACHE_LOCK:
            payload = {code: strings for code, strings in LANG_CACHE.items()}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: _LANG_CACHE_FILE.write_text(text, encoding="utf-8")
            )
    except Exception as exc:
        logging.warning("Failed to persist language cache: %s", exc)


_load_lang_cache_from_disk()


async def translate_text(text: str, target_lang: str = "en", source_lang: Optional[str] = None) -> str:
    target_lang = normalize_language(target_lang)
    source_lang = normalize_language(source_lang) if source_lang else None
    if not text or (target_lang == "en" and source_lang == "en"):
        return text
    cache_key = (text, target_lang, source_lang or "auto")
    cached = TRANSLATION_CACHE.get(cache_key)
    if cached:
        return cached
    # Try primary provider
    providers = [
        ("gtx", "https://translate.googleapis.com/translate_a/single"),
        ("mymemory", "https://api.mymemory.translated.net/get"),
        ("primary", LIBRETRANSLATE_URL),
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
                        translated = data[0][0][0] or text
                        TRANSLATION_CACHE[cache_key] = translated
                        return translated
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
                        TRANSLATION_CACHE[cache_key] = translated
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
                    translated = data.get("translatedText") or text
                    TRANSLATION_CACHE[cache_key] = translated
                    return translated
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
    await _persist_lang_cache()
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
