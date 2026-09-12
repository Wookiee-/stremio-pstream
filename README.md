# stremio-pstream

FastAPI Stremio addon that scrapes P-Stream-style sources into **direct**
stream URLs with **resolutions** and **server names**.

**Zero video bandwidth on the VPS.** The server only fetches lightweight
JSON / HTML / HLS-playlist *text* (a few KB) to discover direct URLs, then
hands those URLs to the Stremio client, which streams straight from the
source. No video is proxied through this server.

## How it works

P-Stream's player resolves a TMDB id through its providers into a raw HLS
URL, signs it via its `eve/ava` proxy (`/sign` -> `fragment?url=..&exp=..&sig=..`),
and plays it. This addon does the server-side equivalent in Python:

1. Stremio calls `GET /stream/{movie|series}/{tt...[:s:e]}.json`.
2. The addon maps the IMDb id to a TMDB id (Cinemeta, then TMDB `/find`
   fallback — JSON only).
3. Each provider scrapes its upstream API -> embed page -> master `.m3u8`
   (text only) and returns **one entry per resolution** with the server name.
4. Stremio plays the direct rendition URLs with the required
   `Referer`/`Origin` headers (sent via `behaviorHints.headers`).

Current providers (`app/providers/`, fanned out concurrently per request):

| Server | Movies | Series | Output |
|---|---|---|---|
| VixSrc | yes | yes | 1080p / 720p / 480p HLS + subtitles |
| VidRock Nova / Atlas / Luna / Orion / Astra | yes | yes | per-server resolutions (HLS masters or MP4 quality lists) |

VidRock notes: its `/api` returns AES-256-GCM-encrypted server URLs
(12-byte IV prefix, base64url). The key is VidRock's own public frontend
key — its browser player runs the same derivation on every playback — and
each decrypted URL is resolved like its player does: JSON
`[{resolution, url}]` lists become one stream per quality, HLS masters
become one stream per rendition, all returned with the `Referer` the
upstreams gate on (passed to Stremio via `behaviorHints.headers`).
Server availability varies per title (same as on P-Stream itself).

## Provider coverage

P-Stream aggregates ~40 providers, but only two are usable for direct
(client-side) links right now:

- **VixSrc + VidRock** (shipped) — plain HLS/MP4 behind a `Referer` check,
  which Stremio can send via `behaviorHints.headers`.
- **vidsrc.to chain** (cracked, not shipped) — full flow reversed
  (`vs_src.php` → embed `CFG` → `metaApi&stream_urls` → per-window WASM
  ChaCha20 decrypt → per-host `/generate.php` JWT), but the JWT carries
  `ip_cidr` of the minter, so VPS-minted links won't play on other clients.
  This family needs P-Stream-style proxying, which this addon avoids by design.
- **Rest of the open catalog** (vidsrc.net, soaper, catflix, vidapi.click,
  ridomovies, zoechip, mp4hydra, embed.su, warezcdn, wecima, vidjoy, …) —
  probed Sep 2026: parked domains, dead DNS/TLS, ISP-blocked, "back soon"
  pages, or login-token gated. Re-probe later; each survivor is one new
  module in `PROVIDERS` (`app/main.py`).
- **Closed providers** (Stellar, VidFast, VidLink, …) run through P-Stream's
  signed proxy and can't be mirrored without leeching their infrastructure.

To add a provider, implement `scrape_movie(client, tmdb_id)` /
`scrape_tv(client, tmdb_id, season, episode)` returning
`list[ScrapedStream]` (see `app/providers/base.py`), then call it from
`app/main.py`. VidRock (`vidrock.net/api/...`) is the natural next one —
its per-server URLs are obfuscated and need its frontend cipher reversed.

## Run

```bash
pip install -r requirements.txt
granian --interface asgi --host 0.0.0.0 --port 7003 app.main:app
```

Or with Docker (both files serve the addon on 7003; `Dockerfile` is the
slim image, `Dockerfile.python` the full-CPython-base variant):

```bash
docker build -t stremio-pstream .
docker run -p 7003:7003 stremio-pstream

# full-base variant:
docker build -f Dockerfile.python -t stremio-pstream:full .
docker run -p 7003:7003 stremio-pstream:full
```

Install in Stremio with the manifest URL:

```text
http://YOUR-VPS-IP:7003/manifest.json
```

Stack: Granian (ASGI server, no uvicorn) + shared httpx2 upstream client
capped at 10 max connections / 5 keepalive (`app/http.py`), so concurrent
Stremio requests don't block each other. Providers add a circuit breaker
(`app/providers/vixsrc.py`): on upstream 5xx / connect errors the provider
backs off for 30s instead of hammering it (429s never trip it — rate-limit,
not outage). Failures are logged, never silently swallowed.

Optional env vars:

| Var | Default | Purpose |
|---|---|---|
| `ADDON_ID` | `org.pstream.stremio` | Stremio addon id |
| `ADDON_NAME` | `P-Stream Direct` | Display name |
| `CACHE_TTL` | `300` | Stream-result cache seconds |
| `PROVIDER_TIMEOUT` | `25` | Per-provider scrape timeout |
| `TMDB_API_KEY` | P-Stream frontend public key | IMDb -> TMDB mapping |
| `UPSTREAM_MAX_CONNECTIONS` | `10` | httpx2 pool: max upstream connections |
| `UPSTREAM_MAX_KEEPALIVE` | `5` | httpx2 pool: max keepalive connections |

## Endpoints

- `GET /manifest.json` — Stremio manifest
- `GET /stream/movie/{tt}.json` — e.g. `/stream/movie/tt0076759.json`
- `GET /stream/series/{tt:s:e}.json` — e.g. `/stream/series/tt9288030:1:1.json`
- `GET /health`
