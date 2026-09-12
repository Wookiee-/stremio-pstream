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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .http import get_client, lifespan
from .providers import vixsrc, vidrock
from .providers.base import ScrapedStream
from .resolve import imdb_to_tmdb, split_stremio_id, to_tmdb

logging.basicConfig(level=logging.INFO)
# httpx logs every upstream GET at INFO - noisy
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("stremio-pstream")

ADDON_ID = os.getenv("ADDON_ID", "org.pstream.stremio")
ADDON_NAME = os.getenv("ADDON_NAME", "P-Stream Direct")
ADDON_VERSION = "1.0.0"
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
PER_PROVIDER_TIMEOUT = int(os.getenv("PROVIDER_TIMEOUT", "25"))

# Player-facing request headers. Proven live against every upstream: without
# these, VidRock hosts 403 and VixSrc embeds refuse. Sent two ways so playback
# works however the client fetches:
#   behaviorHints.headers               -> direct playback (desktop/Android/mpv)
#   behaviorHints.proxyHeaders.request  -> via Stremio's proxy (Web / thin clients)
PLAYER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
)

_cache: dict[str, tuple[float, list[dict]]] = {}


def manifest() -> dict:
    return {
        "id": ADDON_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": "Direct HLS links with resolutions per server (P-Stream style). No video proxied via VPS.",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "imdb:", "tmdb:"],
        "catalogs": [],
        "behaviorHints": {"configurable": False},
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ADDON_NAME} - Stremio Addon</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#1a1a2e;color:#eee;min-height:100vh;display:flex;align-items:center;justify-content:center}}.container{{max-width:600px;padding:40px;text-align:center}}h1{{font-size:2.5rem;margin-bottom:10px;color:#7b2ff7}}.subtitle{{color:#aaa;margin-bottom:30px;font-size:1.1rem}}.card{{background:#16213e;border-radius:12px;padding:30px;margin-bottom:20px}}.install-btn{{display:inline-block;background:#7b2ff7;color:white;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:1.1rem;font-weight:600}}code{{background:#0f3460;padding:2px 8px;border-radius:4px}}</style>
</head><body><div class="container">
<h1>&#9654; {ADDON_NAME}</h1>
<p class="subtitle">FastAPI + Granian + HTTP/2 &middot; direct streams</p>
<div class="card"><a href="stremio:///manifest.json" class="install-btn">Install in Stremio</a>
<p style="color:#888;margin-top:15px">Direct upstream URLs with Referer via proxyHeaders &mdash; no server bandwidth.</p></div>
<div class="card"><h3>Manual install</h3><p><code>/manifest.json</code> on this host</p></div>
</div></body></html>""")


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
    log.info("stream request: kind=%s id=%s -> %s s=%s e=%s", kind, sid, imdb, season, episode)
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
            # Keep the quality on a single line in BOTH fields: some clients
            # (e.g. Nuvio) render the name single-line and would cut anything
            # after a newline, hiding the resolution.
            sub_note = f" | {len(s.subtitles)} subs" if s.subtitles else ""
            req_headers = {"User-Agent": PLAYER_UA, **s.headers}
            entry: dict = {
                "name": f"{ADDON_NAME} {s.quality}",
                "title": f"{s.server} {s.quality}{sub_note}",
                "url": s.url,
            }
            hints: dict = {
                "headers": req_headers,
                "proxyHeaders": {"request": req_headers},
            }
            if s.is_hls:
                hints["notWebReady"] = False
            entry["behaviorHints"] = hints
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
    log.info("returning %d direct stream(s) for %s", len(streams), key)
    return JSONResponse({"streams": streams})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/resolve/{kind}/{imdb}.json")
async def resolve_imdb(kind: str, imdb: str):
    """IMDb -> TMDB mapping with title/seasons (diagnose empty results).

    e.g. /resolve/movie/tt0076759.json -> {"tmdb": "11", "title": ...}
    """
    if imdb.endswith(".json"):
        imdb = imdb[: -len(".json")]
    if kind not in ("movie", "series") or not imdb.startswith("tt"):
        return JSONResponse({"error": "use /resolve/{movie|series}/tt....json"})
    client = await get_client()
    return JSONResponse(await imdb_to_tmdb(client, kind, imdb))
