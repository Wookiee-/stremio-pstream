"""VixSrc provider - same family of sources P-Stream aggregates.

Flow (all lightweight text/JSON, no video via VPS):
  1. GET https://vixsrc.to/api/{movie|tv}/... -> {"src": "/embed/..."}
  2. GET https://vixsrc.to/embed/... (Referer vixsrc.to) -> token/expires/base playlist URL
  3. GET master m3u8 -> per-resolution rendition URLs + subtitle tracks

Returns one ScrapedStream per resolution, server="VixSrc".
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from .base import ScrapedStream
from .hls import parse_master, subtitle_tracks

log = logging.getLogger("stremio-pstream.vixsrc")

API = "https://vixsrc.to"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
SERVER = "VixSrc"

_TOKEN_RE = re.compile(r"'token': '([^']+)'")
_EXPIRES_RE = re.compile(r"'expires': '([^']+)'")
_URL_RE = re.compile(r"url: '([^']+)'")

# Circuit breaker: when the upstream is down (5xx / connect errors), back
# off for a short window instead of hammering it on every Stremio request.
# Checked at the top of _scrape. 429s never trip it (rate-limit, not outage).
_down_until: float = 0.0
_CIRCUIT_BACKOFF = 30.0


def _circuit_open() -> bool:
    return time.time() < _down_until


def _trip_circuit(backoff: float = _CIRCUIT_BACKOFF) -> None:
    global _down_until
    _down_until = time.time() + backoff
    log.warning("vixsrc backing off for %.0fs", backoff)


async def _fetch(client: httpx.AsyncClient, url: str, referer: str | None = None) -> httpx.Response:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    r = await client.get(url, headers=headers, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r


async def scrape_movie(client: httpx.AsyncClient, tmdb_or_imdb: str) -> list[ScrapedStream]:
    return await _scrape(client, f"{API}/api/movie/{tmdb_or_imdb}")


async def scrape_tv(
    client: httpx.AsyncClient, tmdb_or_imdb: str, season: int, episode: int
) -> list[ScrapedStream]:
    return await _scrape(client, f"{API}/api/tv/{tmdb_or_imdb}/{season}/{episode}")


async def _scrape(client: httpx.AsyncClient, api_url: str) -> list[ScrapedStream]:
    # Early exit while backing off — don't fire requests with an open circuit.
    if _circuit_open():
        log.info("vixsrc backing off — skipping %s", api_url)
        return []
    try:
        r = await _fetch(client, api_url)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        # Upstream unreachable — open the circuit, fail fast.
        log.warning("vixsrc unreachable: %s", e)
        _trip_circuit()
        return []
    if r.status_code >= 500:
        # Upstream outage (not rate-limit) — back off.
        log.warning("vixsrc %s on %s — backing off", r.status_code, api_url)
        _trip_circuit()
        return []
    data = r.json()
    src = data.get("src")
    if not src:
        return []
    embed_url = src if src.startswith("http") else API + src

    emb = await _fetch(client, embed_url, referer=f"{API}/")
    html = emb.text
    m_tok = _TOKEN_RE.search(html)
    m_exp = _EXPIRES_RE.search(html)
    m_url = _URL_RE.search(html)
    if not (m_tok and m_exp and m_url):
        return []
    master_url = f"{m_url.group(1)}?token={m_tok.group(1)}&expires={m_exp.group(1)}&h=1&lang=en"

    m = await _fetch(client, master_url, referer=f"{API}/")
    if "#EXTM3U" not in m.text:
        return []
    renditions = parse_master(m.text)
    subs = subtitle_tracks(m.text)
    sub_entries = [{"url": s["url"], "lang": s["lang"]} for s in subs]

    out: list[ScrapedStream] = []
    for rend in renditions:
        out.append(
            ScrapedStream(
                server=SERVER,
                quality=rend["quality"],
                url=rend["url"],
                is_hls=True,
                headers={"Referer": f"{API}/", "Origin": API},
                subtitles=sub_entries,
                width=rend["width"],
                height=rend["height"],
                bandwidth=rend["bandwidth"],
            )
        )
    # Fallback: if master has no variants, hand back the master itself.
    if not out:
        out.append(
            ScrapedStream(
                server=SERVER,
                quality="auto",
                url=master_url,
                is_hls=True,
                headers={"Referer": f"{API}/", "Origin": API},
                subtitles=sub_entries,
            )
        )
    return out
