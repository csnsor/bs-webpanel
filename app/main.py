import os
import logging
import asyncio
import time
from typing import Optional
import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response
from config import DISCORD_BOT_TOKEN, DISCORD_REDIRECT_URI
from app.auth import (
    oauth_authorize_url, exchange_code_for_token, store_user_token,
    fetch_discord_user, refresh_session_profile, validate_state_token,
    issue_state_token
)
from app.appeals import (
    handle_appeal_submission, handle_appeal_page, handle_history_page,
    detect_language, get_strings, enforce_ip_rate_limit
)
from app.utils import (
    wants_html, read_user_session, persist_user_session, maybe_persist_session,
    build_user_chip, render_error
)
from app.middleware import add_security_headers_middleware, add_cors_middleware
from app.bot import bot_client

# Set up logging
logging.basicConfig(level=logging.INFO)

# Create FastAPI app
app = FastAPI(title="BlockSpin Appeals Portal")

# Add middleware
add_cors_middleware(app)
add_security_headers_middleware(app)

# HTTP client for external requests
http_client: Optional[httpx.AsyncClient] = None
_temp_http_client: Optional[httpx.AsyncClient] = None

async def app_lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    try:
        yield
    finally:
        if http_client:
            await http_client.aclose()
            http_client = None
        if _temp_http_client:
            await _temp_http_client.aclose()
            _temp_http_client = None

app.router.lifespan_context = app_lifespan

def get_http_client() -> httpx.AsyncClient:
    if http_client:
        return http_client
    global _temp_http_client
    if not _temp_http_client:
        _temp_http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _temp_http_client

@app.on_event("startup")
async def startup_event():
    if not bot_client:
        logging.warning("discord.py not available; bot client not started.")
        raise RuntimeError("discord.py is required for the appeal bot. Please install dependencies.")
    if not DISCORD_BOT_TOKEN:
        logging.warning("DISCORD_BOT_TOKEN missing; bot client not started.")
        raise RuntimeError("DISCORD_BOT_TOKEN missing; bot client cannot start.")
    try:
        await bot_client.login(DISCORD_BOT_TOKEN)
    except Exception as exc:
        logging.exception("Discord bot login failed: %s", exc)
        raise RuntimeError("Discord bot token is invalid or missing required intents.") from exc
    asyncio.create_task(bot_client.connect())

# Exception handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if wants_html(request):
        msg = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
        lang = await detect_language(request)
        strings = await get_strings(lang)
        return HTMLResponse(render_error("Request failed", msg, exc.status_code, lang=lang, strings=strings))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if wants_html(request):
        lang = await detect_language(request)
        strings = await get_strings(lang)
        return HTMLResponse(render_error("Invalid input", "Please check the form and try again.", 422, lang=lang, strings=strings))
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.exception("Unhandled error: %s", exc)
    if wants_html(request):
        lang = await detect_language(request)
        strings = await get_strings(lang)
        return HTMLResponse(render_error("Server error", "Unexpected error. Please try again.", 500, lang=lang, strings=strings))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    lang = await detect_language(request)
    strings = await get_strings(lang)
    session = read_user_session(request)
    
    # Refresh session profile if needed
    refreshed = False
    if session:
        session, refreshed = await refresh_session_profile(session)
    
    # Build user chip if logged in
    user_chip = build_user_chip(session) if session else ""
    
    # Render appropriate content based on authentication status
    if session:
        content = f"""
        <div class="card hero">
            <h1>{strings.get("welcome_back", "Welcome back")}, {session.get('display_name', session.get('uname', ''))}!</h1>
            <p>{strings.get("welcome_back_msg", "You can submit an appeal or check your appeal status.")}</p>
            <div class="btn-row">
                <a href="/appeal" class="btn">{strings.get("review_ban", "Review my ban")}</a>
                <a href="/history" class="btn secondary">{strings.get("status_cta", "View status")}</a>
            </div>
        </div>
        """
    else:
        state = issue_state_token(get_client_ip(request))
        auth_url = oauth_authorize_url(state)
        content = f"""
        <div class="card hero">
            <h1>{strings.get("hero_title", "Appeal your Discord ban.")}</h1>
            <p>{strings.get("hero_sub", "Login to Discord to see details, review recent context, and submit an appeal.")}</p>
            <div class="btn-row">
                <a href="{auth_url}" class="btn">{strings.get("login", "Login with Discord")}</a>
            </div>
        </div>
        
        <div class="card">
            <h2>{strings.get("how_it_works", "How it works")}</h2>
            <div class="steps">
                <div class="step">
                    <div class="step-number">1</div>
                    <div class="step-content">
                        <h3>{strings.get("step_1", "Authenticate with Discord")}</h3>
                        <p>{strings.get("step_1_desc", "Authenticate with Discord to confirm it's your account.")}</p>
                    </div>
                </div>
                <div class="step">
                    <div class="step-number">2</div>
                    <div class="step-content">
                        <h3>{strings.get("step_2", "Review and Submit")}</h3>
                        <p>{strings.get("step_2_desc", "Review ban details, share evidence, and submit securely.")}</p>
                    </div>
                </div>
                <div class="step">
                    <div class="step-number">3</div>
                    <div class="step-content">
                        <h3>{strings.get("step_3", "Track Status")}</h3>
                        <p>{strings.get("step_3_desc", "Stay signed in to monitor your appeal status.")}</p>
                    </div>
                </div>
            </div>
        </div>
        """
    
    # Update response with session if refreshed
    response = HTMLResponse(render_page(strings.get("home_title", "BlockSpin Appeals"), content, lang=lang, strings={**strings, "user_chip": user_chip}))
    maybe_persist_session(response, session, refreshed)
    return response

@app.get("/callback")
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing authorization code or state")
    
    ip = get_client_ip(request)
    if not validate_state_token(state, ip):
        raise HTTPException(status_code=400, detail="Invalid state token")
    
    # Exchange code for token
    token_data = await exchange_code_for_token(code)
    
    # Get user info
    user = await fetch_discord_user(token_data["access_token"])
    
    # Store token for later use
    store_user_token(user.id, token_data)
    
    # Create session
    session = {
        "uid": user.id,
        "uname": f"{user.username}#{user.discriminator}",
        "display_name": user.global_name or user.username,
        "iat": time.time()
    }
    
    # Redirect to home with session
    response = RedirectResponse(url="/", status_code=302)
    persist_user_session(response, session["uid"], session["uname"], session["display_name"])
    return response

@app.get("/appeal", response_class=HTMLResponse)
async def appeal_page(request: Request):
    session = read_user_session(request)
    if not session:
        lang = await detect_language(request)
        strings = await get_strings(lang)
        state = issue_state_token(get_client_ip(request))
        auth_url = oauth_authorize_url(state)
        content = f"""
        <div class="card">
            <h2>{strings.get("login_required", "Login Required")}</h2>
            <p>{strings.get("login_required_msg", "You need to login with Discord to submit an appeal.")}</p>
            <div class="btn-row">
                <a href="{auth_url}" class="btn">{strings.get("login", "Login with Discord")}</a>
            </div>
        </div>
        """
        return HTMLResponse(render_page(strings.get("login_required", "Login Required"), content, lang=lang, strings=strings))
    
    # Refresh session profile if needed
    session, refreshed = await refresh_session_profile(session)
    
    # Get user info
    user_id = session.get("uid")
    token = await get_valid_access_token(user_id)
    if not token:
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")
    
    user = await fetch_discord_user(token)
    
    # Detect language and get strings
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Handle appeal page
    response = await handle_appeal_page(request, session, user, lang, strings)
    maybe_persist_session(response, session, refreshed)
    return response

@app.post("/submit", response_class=HTMLResponse)
async def submit_appeal(
    request: Request,
    appeal_reason: str = Form(...),
):
    session = read_user_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="You must be logged in to submit an appeal.")
    
    # Refresh session profile if needed
    session, refreshed = await refresh_session_profile(session)
    
    # Get user info
    user_id = session.get("uid")
    token = await get_valid_access_token(user_id)
    if not token:
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")
    
    user = await fetch_discord_user(token)
    
    # Detect language and get strings
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Get ban data
    ban_data = await fetch_ban_if_exists(user_id)
    if not ban_data:
        content = f"""
        <div class="card">
            <h2>{strings.get("not_banned", "Not Banned")}</h2>
            <p>{strings.get("not_banned_msg", "You are not currently banned from the server.")}</p>
            <div class="btn-row">
                <a href="/" class="btn secondary">{strings.get("back_home", "Back Home")}</a>
            </div>
        </div>
        """
        response = HTMLResponse(render_page(strings.get("not_banned", "Not Banned"), content, lang=lang, strings=strings))
        maybe_persist_session(response, session, refreshed)
        return response
    
    # Handle appeal submission
    response = await handle_appeal_submission(
        request, session, user, ban_data, appeal_reason, lang, strings
    )
    maybe_persist_session(response, session, refreshed)
    return response

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    session = read_user_session(request)
    if not session:
        lang = await detect_language(request)
        strings = await get_strings(lang)
        state = issue_state_token(get_client_ip(request))
        auth_url = oauth_authorize_url(state)
        content = f"""
        <div class="card">
            <h2>{strings.get("login_required", "Login Required")}</h2>
            <p>{strings.get("login_required_msg", "You need to login with Discord to view your appeal history.")}</p>
            <div class="btn-row">
                <a href="{auth_url}" class="btn">{strings.get("login", "Login with Discord")}</a>
            </div>
        </div>
        """
        return HTMLResponse(render_page(strings.get("login_required", "Login Required"), content, lang=lang, strings=strings))
    
    # Refresh session profile if needed
    session, refreshed = await refresh_session_profile(session)
    
    # Get user info
    user_id = session.get("uid")
    token = await get_valid_access_token(user_id)
    if not token:
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")
    
    user = await fetch_discord_user(token)
    
    # Detect language and get strings
    lang = await detect_language(request)
    strings = await get_strings(lang)
    
    # Handle history page
    response = await handle_history_page(request, session, user, lang, strings)
    maybe_persist_session(response, session, refreshed)
    return response

@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("bs_session")
    return response