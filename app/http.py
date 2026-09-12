"""Shared upstream HTTP client (httpx, HTTP/2 where supported).

Single pooled AsyncClient for all scraping traffic: max 10 connections,
5 keepalive — video itself is never fetched, only JSON/HTML/playlist text.
Recreated if the running event loop changes or it was closed, so worker
restarts can never strand requests on a dead pool.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import httpx

MAX_CONNECTIONS = int(os.getenv("UPSTREAM_MAX_CONNECTIONS", "10"))
MAX_KEEPALIVE = int(os.getenv("UPSTREAM_MAX_KEEPALIVE", "5"))

_client: httpx.AsyncClient | None = None
_client_loop: object | None = None


def _create() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,  # HTTP/2 where supported, HTTP/1.1 fallback otherwise
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE,
        ),
        timeout=httpx.Timeout(15.0, read=30.0),
        follow_redirects=True,
        headers={"User-Agent": "stremio-pstream/1.0"},
    )


async def get_client() -> httpx.AsyncClient:
    """Shared client; (re)created lazily and per event loop."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop or _client.is_closed:
        if _client is not None and not _client.is_closed:
            try:
                await _client.aclose()
            except Exception:
                pass
        _client = _create()
        _client_loop = loop
    return _client


@asynccontextmanager
async def lifespan(_app):
    global _client
    _client = _create()
    try:
        yield
    finally:
        if _client is not None:
            try:
                await _client.aclose()
            except Exception:
                pass
            _client = None
