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
    hit = await imdb_to_tmdb(client, kind, imdb)
    return hit["tmdb"] or imdb


async def imdb_to_tmdb(client: httpx2.AsyncClient, kind: str, imdb: str) -> dict:
    """Full IMDb -> TMDB mapping with title/seasons for diagnostics.

    Chain: Cinemeta meta (moviedb_id + name) -> TMDB /find by IMDb id ->
    TMDB /search by title from Cinemeta. Returns dict with tmdb (str|None),
    title, year and, for series, seasons + the source that matched.
    """
    out: dict = {"imdb": imdb, "kind": kind, "tmdb": None,
                 "title": None, "year": None, "via": None, "seasons": []}
    title_hint: str | None = None

    # 1. Cinemeta (Stremio's own metadata).
    try:
        r = await client.get(f"{CINEMETA}/{kind}/{imdb}.json", headers=UA, timeout=10)
        r.raise_for_status()
        meta = r.json().get("meta", {})
        if meta.get("moviedb_id"):
            out["tmdb"] = str(meta["moviedb_id"])
            out["via"] = "cinemeta"
        if meta.get("name"):
            title_hint = meta["name"]
    except Exception:
        pass

    # 2. TMDB /find by external IMDb id.
    if not out["tmdb"]:
        try:
            r = await client.get(
                f"{TMDB_API}/find/{imdb}",
                params={"api_key": TMDB_API_KEY, "external_source": "imdb_id"},
                headers=UA,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("movie_results" if kind == "movie" else "tv_results", [])
            if results:
                out["tmdb"] = str(results[0]["id"])
                out["via"] = "tmdb-find"
        except Exception:
            pass

    # 3. TMDB /search by title (from Cinemeta) as last resort.
    if not out["tmdb"] and title_hint:
        try:
            r = await client.get(
                f"{TMDB_API}/search/{'movie' if kind == 'movie' else 'tv'}",
                params={"api_key": TMDB_API_KEY, "query": title_hint},
                headers=UA,
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if results:
                out["tmdb"] = str(results[0]["id"])
                out["via"] = "tmdb-search"
        except Exception:
            pass

    if not out["tmdb"]:
        return out

    # 4. Details: title/year (+ season list for series) to verify episodes exist.
    try:
        r = await client.get(
            f"{TMDB_API}/{'movie' if kind == 'movie' else 'tv'}/{out['tmdb']}",
            params={"api_key": TMDB_API_KEY},
            headers=UA,
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        out["title"] = d.get("title" if kind == "movie" else "name")
        date = d.get("release_date" if kind == "movie" else "first_air_date") or ""
        out["year"] = date[:4] or None
        if kind == "series":
            out["seasons"] = [
                {"n": s.get("season_number"), "episodes": s.get("episode_count")}
                for s in d.get("seasons", [])
            ]
    except Exception:
        pass
    return out
