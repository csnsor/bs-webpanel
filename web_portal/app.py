from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .bot import bot_client, heartbeat, run_bot_forever
from .clients import close_http_clients, init_http_client
from .i18n import detect_language, get_strings
from .routers.health import router as health_router
from .routers.interactions import router as interactions_router
from .routers.pages import router as pages_router
from .routers.status_api import router as status_router
from .services.sessions import serializer
from .settings import validate_required_envs
from . import state
from .ui import render_error
from .utils import wants_html


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    await init_http_client()

    if not bot_client:
        logging.warning("discord.py not available; bot client not started.")
        raise RuntimeError("discord.py is required for the appeal bot. Please install dependencies.")

    if not state._bot_task or state._bot_task.done():
        state._bot_task = asyncio.create_task(run_bot_forever())

    if not state._bot_heartbeat_task or state._bot_heartbeat_task.done():
        state._bot_heartbeat_task = asyncio.create_task(heartbeat())

    try:
        yield
    finally:
        if bot_client:
            try:
                await bot_client.close()
            except Exception:
                pass
        if state._bot_task and not state._bot_task.done():
            state._bot_task.cancel()
        if state._bot_heartbeat_task and not state._bot_heartbeat_task.done():
            state._bot_heartbeat_task.cancel()
        await close_http_clients()


def create_app() -> FastAPI:
    validate_required_envs()

    logging.basicConfig(level=logging.INFO)

    app = FastAPI(title="BlockSpin Appeals Portal", lifespan=app_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # If a prior handler marked the request for forced logout, clear the session cookie
        if getattr(request.state, "force_logout", False):
            response.delete_cookie("bs_session")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if wants_html(request):
            lang = await detect_language(request)
            strings = await get_strings(lang)
            if exc.status_code == 404:
                return render_error(
                    "Page not found",
                    "We couldn't find that page. Check the link or return home.",
                    status_code=404,
                    lang=lang,
                    strings=strings,
                )
            msg = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
            return render_error("Request failed", msg, status_code=exc.status_code, lang=lang, strings=strings)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        if wants_html(request):
            lang = await detect_language(request)
            strings = await get_strings(lang)
            return render_error("Invalid input", "Please check the form and try again.", status_code=422, lang=lang, strings=strings)
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logging.exception("Unhandled error: %s", exc)
        if wants_html(request):
            lang = await detect_language(request)
            strings = await get_strings(lang)
            return render_error("Server error", "Unexpected error. Please try again.", status_code=500, lang=lang, strings=strings)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.include_router(health_router)
    app.include_router(status_router)
    app.include_router(interactions_router)
    app.include_router(pages_router)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Export serializer (used for state/session signing) so other modules can import via app if needed.
    app.state.serializer = serializer

    return app


app = create_app()
