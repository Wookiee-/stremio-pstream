"""Shared types. Only scraping traffic touches the VPS; video streams direct."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ScrapedStream(BaseModel):
    server: str = Field(description="Source/server name, e.g. VixSrc")
    quality: str = Field(description="Resolution label, e.g. 1080p")
    url: str = Field(description="Direct HLS/MP4 URL - client streams this, not the VPS")
    is_hls: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    subtitles: list[dict] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
