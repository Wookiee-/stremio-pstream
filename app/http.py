"""Shared upstream HTTP client (httpx2).

Single pooled AsyncClient for all scraping traffic: max 10 connections,
5 keepalive — video itself is never fetched, only JSON/HTML/playlist text.
Managed via FastAPI lifespan so connections are reused, not per-request.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import httpx2

MAX_CONNECTIONS = int(os.getenv("UPSTREAM_MAX_CONNECTIONS", "10"))
MAX_KEEPALIVE = int(os.getenv("UPSTREAM_MAX_KEEPALIVE", "5"))

_client: httpx2.AsyncClient | None = None
_lock = asyncio.Lock()


def _create() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        limits=httpx2.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE,
        ),
        timeout=httpx2.Timeout(20.0),
        follow_redirects=True,
        headers={"User-Agent": "stremio-pstream/1.0"},
    )


async def get_client() -> httpx2.AsyncClient:
    """Shared client; created lazily so it works even if the server
    never runs the ASGI lifespan protocol."""
    global _client
    if _client is None:
        async with _lock:
            if _client is None:
                _client = _create()
    return _client


@asynccontextmanager
async def lifespan(_app):
    await get_client()
    try:
        yield
    finally:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None
