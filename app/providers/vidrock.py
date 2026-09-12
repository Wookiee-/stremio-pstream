"""VidRock provider - one of the sources P-Stream aggregates ("Rosa").

Flow (all lightweight text/JSON, no video via VPS):
  1. GET https://vidrock.net/api/{movie/{id}|tv/{id}/{s}/{e}}
     -> {"Luna": {"url": "<enc>", "type": "hls", ...}, ...}
  2. Decrypt each server URL with AES-256-GCM (12-byte IV prefix, base64url).
     The key is VidRock's own public frontend key — the browser player runs
     this exact derivation (bQ(xQ) -> AES-GCM) for every playback.
  3. GET the decrypted URL with the vidrock Referer (upstreams 403 without
     it — browsers send it automatically):
       - JSON [{resolution, url}] (their TQ() path) -> one stream per quality
       - HLS master playlist -> one stream per rendition (relative URIs joined)
       - otherwise -> single stream typed by the API ("hls"/"mp4")

Returns one ScrapedStream per resolution per server.
"""
from __future__ import annotations

import base64
import logging
import time
from urllib.parse import urljoin

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import ScrapedStream
from .hls import parse_master

log = logging.getLogger("stremio-pstream.vidrock")

API = "https://vidrock.net/api"
REFERER = "https://vidrock.net/"
# Public frontend key shipped in VidRock's own player bundle (hex -> AES-256).
KEY = bytes.fromhex(
    "7f3e9c2a8b5d1f4e6a9c3b7d2e5f8a1c4b6d9e2f5a8c1b4d7e9f2a5c8b1d4e7f"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Circuit breaker: back off on 5xx / connect errors, never on 429s.
_down_until: float = 0.0
_CIRCUIT_BACKOFF = 30.0


def _circuit_open() -> bool:
    return time.time() < _down_until


def _trip_circuit(backoff: float = _CIRCUIT_BACKOFF) -> None:
    global _down_until
    _down_until = time.time() + backoff
    log.debug("vidrock backing off for %.0fs", backoff)


def decrypt_url(enc: str) -> str:
    raw = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4))
    if len(raw) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(KEY).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def _headers() -> dict[str, str]:
    return {"User-Agent": UA, "Referer": REFERER, "Origin": "https://vidrock.net"}


def _player_headers() -> dict[str, str]:
    # Handed to Stremio so it passes the upstream Referer gate.
    return {"Referer": REFERER, "Origin": "https://vidrock.net"}


async def scrape_movie(client: httpx.AsyncClient, tmdb_or_imdb: str) -> list[ScrapedStream]:
    return await _scrape(client, f"{API}/movie/{tmdb_or_imdb}")


async def scrape_tv(
    client: httpx.AsyncClient, tmdb_or_imdb: str, season: int, episode: int
) -> list[ScrapedStream]:
    return await _scrape(client, f"{API}/tv/{tmdb_or_imdb}/{season}/{episode}")


async def _scrape(client: httpx.AsyncClient, api_url: str) -> list[ScrapedStream]:
    if _circuit_open():
        log.info("vidrock backing off — skipping %s", api_url)
        return []
    try:
        r = await client.get(api_url, headers={"User-Agent": UA}, timeout=20)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        log.debug("vidrock unreachable: %s", e)
        _trip_circuit()
        return []
    if r.status_code >= 500:
        log.debug("vidrock %s on %s — backing off", r.status_code, api_url)
        _trip_circuit()
        return []
    if r.status_code != 200:
        return []

    out: list[ScrapedStream] = []
    try:
        servers = r.json()
    except Exception:
        return []
    for name, info in servers.items():
        if not isinstance(info, dict) or not info.get("url"):
            continue
        try:
            plain = decrypt_url(info["url"])
        except Exception as e:
            log.debug("vidrock decrypt failed for %s: %s", name, e)
            continue
        out.extend(await _resolve_source(client, name, info, plain))
    # highest resolution first within each server, servers in API order
    return out


async def _resolve_source(
    client: httpx.AsyncClient, server: str, info: dict, url: str
) -> list[ScrapedStream]:
    try:
        r = await client.get(url, headers=_headers(), timeout=20)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        log.debug("vidrock source %s unreachable: %s", server, e)
        return []
    if r.status_code != 200:
        return []
    ctype = r.headers.get("content-type", "")

    # TQ() path: JSON quality list [{resolution, url}].
    if "json" in ctype:
        try:
            items = r.json()
        except Exception:
            return []
        if isinstance(items, list) and items and isinstance(items[0], dict) and "url" in items[0]:
            streams = [
                ScrapedStream(
                    server=server,
                    quality=f"{it.get('resolution', 'auto')}p"
                    if isinstance(it.get("resolution"), int)
                    else str(it.get("resolution", "auto")),
                    url=it["url"],
                    is_hls=False,
                    headers=_player_headers(),
                )
                for it in items
                if isinstance(it, dict) and it.get("url")
            ]
            streams.sort(
                key=lambda s: int(s.quality.rstrip("p")) if s.quality.rstrip("p").isdigit() else 0,
                reverse=True,
            )
            return streams
        return []

    # HLS master playlist path.
    if "#EXTM3U" in r.text[:1000]:
        renditions = parse_master(r.text)
        if not renditions:
            return [
                ScrapedStream(
                    server=server, quality="auto", url=url,
                    is_hls=True, headers=_player_headers(),
                )
            ]
        return [
            ScrapedStream(
                server=server,
                quality=rend["quality"],
                url=urljoin(url, rend["url"]),
                is_hls=True,
                headers=_player_headers(),
                width=rend["width"],
                height=rend["height"],
                bandwidth=rend["bandwidth"],
            )
            for rend in renditions
        ]

    # Opaque single file (mp4/hls by API type hint).
    kind = (info.get("type") or "").lower()
    return [
        ScrapedStream(
            server=server,
            quality="auto",
            url=url,
            is_hls=(kind == "hls"),
            headers=_player_headers(),
        )
    ]
