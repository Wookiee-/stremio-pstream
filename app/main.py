"""P-Stream style Stremio addon (FastAPI).

Zero video bandwidth on the VPS: endpoints only fetch JSON/HTML/HLS-playlist
TEXT to discover direct stream URLs, then hand those URLs to the Stremio
client which streams straight from the source.

Stremio protocol:
  GET /manifest.json
  GET /stream/movie/{id}.json
  GET /stream/series/{id:s:e}.json
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from .http import get_client, lifespan
from .providers import vixsrc, vidrock
from .providers.base import ScrapedStream
from .resolve import split_stremio_id, to_tmdb

log = logging.getLogger("stremio-pstream")

ADDON_ID = os.getenv("ADDON_ID", "org.pstream.stremio")
ADDON_NAME = os.getenv("ADDON_NAME", "P-Stream Direct")
ADDON_VERSION = "1.0.0"
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
PER_PROVIDER_TIMEOUT = int(os.getenv("PROVIDER_TIMEOUT", "25"))

PROVIDERS = (vixsrc, vidrock)


async def _scrape_provider(client, provider, kind, tmdb, imdb, season, episode):
    """One provider: TMDB id first, raw IMDb fallback. Never raises."""
    try:
        async with asyncio.timeout(PER_PROVIDER_TIMEOUT):
            if kind == "movie":
                out = await provider.scrape_movie(client, tmdb)
                if not out and tmdb != imdb:
                    out = await provider.scrape_movie(client, imdb)
            else:
                if season is None or episode is None:
                    return []
                out = await provider.scrape_tv(client, tmdb, season, episode)
                if not out and tmdb != imdb:
                    out = await provider.scrape_tv(client, imdb, season, episode)
            return out
    except Exception as e:
        # Don't spam logs on rate-limits — degrade visibly but quietly.
        # 429s never trip breakers (rate-limit, not outage).
        if "429" in str(e):
            log.debug("%s 429 suppressed", provider.__name__)
        else:
            log.warning("%s error: %s", provider.__name__, e)
        return []

app = FastAPI(title=ADDON_NAME, lifespan=lifespan)

_cache: dict[str, tuple[float, list[dict]]] = {}


def manifest() -> dict:
    return {
        "id": ADDON_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": "Direct HLS links with resolutions per server (P-Stream style). No video proxied via VPS.",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
        "behaviorHints": {"configurable": False},
    }


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse("/manifest.json")


@app.get("/manifest.json")
async def get_manifest():
    return JSONResponse(manifest())


@app.get("/stream/{kind}/{sid}.json")
async def get_stream(kind: str, sid: str):
    # Stremio may URL-encode the trailing .json handling; FastAPI strips it via param.
    if sid.endswith(".json"):
        sid = sid[: -len(".json")]
    if kind not in ("movie", "series"):
        return JSONResponse({"streams": []})
    key = f"{kind}:{sid}"
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return JSONResponse({"streams": _cache[key][1]})

    imdb, season, episode = split_stremio_id(sid)
    streams: list[dict] = []
    try:
        client = await get_client()
        tmdb = await to_tmdb(client, "movie" if kind == "movie" else "series", imdb)
        # Fan out across providers concurrently; each never raises.
        scraped_lists = await asyncio.gather(
            *(_scrape_provider(client, p, kind, tmdb, imdb, season, episode) for p in PROVIDERS)
        )
        scraped: list[ScrapedStream] = [s for lst in scraped_lists for s in lst]
        for s in scraped:
            tag = f"{s.quality} {s.server}"
            entry: dict = {
                "name": f"{ADDON_NAME}\n{s.server} {s.quality}",
                "title": tag,
                "url": s.url,
            }
            if s.is_hls:
                entry["behaviorHints"] = {
                    "headers": s.headers,
                    "notWebReady": False,
                }
            else:
                entry["behaviorHints"] = {"headers": s.headers}
            if s.subtitles:
                entry["subtitles"] = [
                    {"url": sub["url"], "lang": sub.get("lang", "en")}
                    for sub in s.subtitles
                ]
            streams.append(entry)
    except Exception as e:
        log.warning("stream handler error for %s: %s", key, e)
        streams = []

    _cache[key] = (now, streams)
    return JSONResponse({"streams": streams})


@app.get("/health")
async def health():
    return {"ok": True}
