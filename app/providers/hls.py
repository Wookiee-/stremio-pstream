"""Parse HLS master playlists into per-resolution renditions.

Only the playlist TEXT is fetched (a few KB). No video segments are
downloaded or proxied - the VPS never touches media bandwidth.
"""
from __future__ import annotations

import re

_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)")
_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=(?:"([^"]+)"|([^,]+))')


def parse_master(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _STREAM_INF_RE.finditer(text):
        attr_str, uri = m.group(1), m.group(2).strip()
        attrs = {k: (v1 if v1 else v2) for k, v1, v2 in _ATTR_RE.findall(attr_str)}
        res = attrs.get("RESOLUTION", "")
        w = h = None
        if "x" in res:
            try:
                w, h = (int(x) for x in res.split("x"))
            except ValueError:
                pass
        label = f"{h}p" if h else attrs.get("NAME", "auto")
        try:
            bw = int(attrs.get("BANDWIDTH", 0)) or None
        except ValueError:
            bw = None
        out.append(
            {
                "url": uri,
                "quality": label,
                "width": w,
                "height": h,
                "bandwidth": bw,
                "codecs": attrs.get("CODECS"),
            }
        )
    # highest resolution first
    out.sort(key=lambda r: (r["height"] or 0, r["bandwidth"] or 0), reverse=True)
    return out


def parse_subtitles(text: str) -> list[dict]:
    subs: list[dict] = []
    for line in text.splitlines():
        if "TYPE=SUBTITLES" not in line:
            continue
        attrs = {k: (v1 if v1 else v2) for k, v1, v2 in _ATTR_RE.findall(line)}
        # URI is on the following line for EXT-X-MEDIA
        subs.append(
            {
                "lang": attrs.get("LANGUAGE", "und"),
                "name": attrs.get("NAME", attrs.get("LANGUAGE", "sub")),
            }
        )
    return subs


def subtitle_tracks(text: str) -> list[dict]:
    """Extract subtitle track URLs from a master playlist."""
    tracks: list[dict] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "TYPE=SUBTITLES" not in line:
            continue
        attrs = {k: (v1 if v1 else v2) for k, v1, v2 in _ATTR_RE.findall(line)}
        uri = None
        if "URI=" in line:
            uri = attrs.get("URI")
        elif i + 1 < len(lines) and not lines[i + 1].startswith("#"):
            uri = lines[i + 1].strip()
        if uri:
            tracks.append(
                {"url": uri, "lang": attrs.get("LANGUAGE", "und"),
                 "name": attrs.get("NAME", attrs.get("LANGUAGE", "sub"))}
            )
    return tracks
