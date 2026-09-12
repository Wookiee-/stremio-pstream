"""Resolve Stremio IDs to upstream IDs.

Stremio sends `tt...` (+ `:s:e` for series). Most upstreams accept IMDb
directly, but TMDB numeric IDs work more broadly (and match P-Stream URLs),
so prefer TMDB via Cinemeta with graceful fallback to the raw id.
"""
from __future__ import annotations

import httpx2
import os

CINEMETA = "https://v3-cinemeta.strem.io/meta"
TMDB_API = "https://api.themoviedb.org/3"
# Public v3 key shipped in P-Stream's own frontend; override with env TMDB_API_KEY.
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "db55323b8d3e4154498498a75642b381")
UA = {"User-Agent": "stremio-pstream/1.0"}


def split_stremio_id(stream_id: str) -> tuple[str, int | None, int | None]:
    parts = stream_id.split(":")
    imdb = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None
    return imdb, season, episode


async def to_tmdb(client: httpx2.AsyncClient, kind: str, imdb: str) -> str:
    """Return TMDB numeric id as str, or the original imdb on failure."""
    if not imdb.startswith("tt"):
        return imdb  # already a TMDB id
    try:
        r = await client.get(f"{CINEMETA}/{kind}/{imdb}.json", headers=UA, timeout=10)
        r.raise_for_status()
        mid = r.json().get("meta", {}).get("moviedb_id")
        if mid:
            return str(mid)
    except Exception:
        pass
    # Fallback: TMDB /find with IMDb id (tiny JSON, no media bandwidth).
    try:
        r = await client.get(
            f"{TMDB_API}/find/{imdb}",
            params={"api_key": TMDB_API_KEY, "external_source": "imdb_id"},
            headers=UA,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if kind == "movie" and data.get("movie_results"):
            return str(data["movie_results"][0]["id"])
        if kind == "series" and data.get("tv_results"):
            return str(data["tv_results"][0]["id"])
    except Exception:
        pass
    return imdb
