from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Brand(BaseModel):
    name: str
    voice: str
    languages: list[str] = ["en"]
    audience: str


class ModelSettings(BaseModel):
    id: str = "claude-opus-5-5"
    effort: dict[str, Literal["low", "medium", "high", "xhigh", "max"]] = Field(default_factory=dict)

    def effort_for(self, stage: str) -> str:
        return self.effort.get(stage, "high")


class Content(BaseModel):
    posts_per_day: int = 1
    formats: list[str] = ["short_video", "carousel", "quote_card"]
    verse_selection: Literal["sequential", "strategic"] = "strategic"
    ai_disclosure: str
    depiction_rules: list[str]


class Review(BaseModel):
    min_fidelity_score: int = 8
    require_human_approval: bool = True


class Schedule(BaseModel):
    timezone: str = "UTC"
    slots: list[str] = ["07:00"]


class Platform(BaseModel):
    enabled: bool = True
    aspect: str = "9:16"


class Provider(BaseModel):
    provider: str


class FalSettings(BaseModel):
    image_model: str = "fal-ai/nano-banana-2"
    video_model: str = "fal-ai/veo3.1/fast/image-to-video"
    image_resolution: Literal["0.5K", "1K", "2K", "4K"] = "1K"
    video_resolution: Literal["720p", "1080p", "4k"] = "720p"
    # Narration is our own voice track, so clips are rendered silent (also cheaper).
    video_audio: bool = False
    # Hard ceiling on fal spend per UTC day. Assets that would cross it are skipped, not rendered.
    daily_budget_usd: float = 10.0
    # List prices used to estimate spend before each call. Update if fal changes pricing.
    image_price_usd: float = 0.08
    video_price_per_second_usd: float = 0.10
    poll_seconds: float = 5.0
    timeout_seconds: float = 900.0


class MediaSettings(Provider):
    fal: FalSettings = FalSettings()


class Paths(BaseModel):
    db: str = "studio.db"
    verses: str = "data/verses.json"
    outbox: str = "outbox"
    media: str = "media"


class Config(BaseModel):
    brand: Brand
    model: ModelSettings = ModelSettings()
    content: Content
    review: Review = Review()
    schedule: Schedule = Schedule()
    platforms: dict[str, Platform]
    media: MediaSettings = MediaSettings(provider="prompt_only")
    publishing: Provider = Provider(provider="outbox")
    paths: Paths = Paths()

    @property
    def enabled_platforms(self) -> dict[str, Platform]:
        return {name: p for name, p in self.platforms.items() if p.enabled}


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))
