from __future__ import annotations

from typing import Optional

import httpx
from jinja2 import Environment, select_autoescape

http_client: Optional[httpx.AsyncClient] = None
_temp_http_client: Optional[httpx.AsyncClient] = None

JINJA_ENV = Environment(autoescape=select_autoescape(default_for_string=True, default=True))


async def init_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return http_client


def get_http_client() -> httpx.AsyncClient:
    if http_client:
        return http_client
    global _temp_http_client
    if not _temp_http_client:
        _temp_http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _temp_http_client


async def close_http_clients() -> None:
    global http_client, _temp_http_client
    if http_client:
        await http_client.aclose()
        http_client = None
    if _temp_http_client:
        await _temp_http_client.aclose()
        _temp_http_client = None

