from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class Channel(BaseModel):
    name: str
    handle: str = ""


class ReelSettings(BaseModel):
    min_seconds: float = 15
    max_seconds: float = 30
    fps: int = 24
    width: int = 1080
    height: int = 1920
    narration: bool = False
    voice: str = "en-IN-PrabhatNeural"
    verse_line: bool = True
    rotated_wide_shots: bool = True
    preset: str = "slow"           # x264 preset for the master; "ultrafast" for quick drafts and tests


class ClaudeSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort_write: str = "high"
    effort_review: str = "high"
    effort_qc: str = "medium"
    min_review_score: int = 8
    min_qc_score: int = 7


class VideoModel(BaseModel):
    id: str
    price_per_second: float
    durations: list[int] = [4, 6, 8]
    duration_format: str = "{n}s"
    resolution: str = "720p"
    native_audio: bool = False

    def pick_duration(self, wanted: float) -> int:
        return next((d for d in sorted(self.durations) if d >= wanted), max(self.durations))


class FalSettings(BaseModel):
    keyframe_model: str
    keyframe_refs_field: str = "image_urls"
    refs_model: str
    video_models: list[VideoModel]
    keyframe_price: float = 0.08
    daily_budget_usd: float = 15
    reel_budget_usd: float = 8
    max_retries_per_shot: int = 2
    poll_seconds: float = 5
    timeout_seconds: float = 900


class PublishSettings(BaseModel):
    auto_publish: bool = True
    privacy: Literal["public", "unlisted", "private"] = "public"
    platforms: list[str] = ["youtube"]


class Paths(BaseModel):
    verses: str = "data/verses.json"
    style: str = "style"
    music: str = "music"
    sfx: str = "sfx"
    out: str = "reels"
    cache: str = ".cache/fal"
    state: str = "reels_state.json"


class Settings(BaseModel):
    channel: Channel
    reel: ReelSettings = ReelSettings()
    claude: ClaudeSettings = ClaudeSettings()
    fal: FalSettings
    publish: PublishSettings = PublishSettings()
    paths: Paths = Paths()

    def style_bible(self) -> dict:
        return json.loads((Path(self.paths.style) / "style_bible.json").read_text(encoding="utf-8"))

    def characters(self) -> dict[str, str]:
        return json.loads((Path(self.paths.style) / "characters.json").read_text(encoding="utf-8"))

    def episodes(self) -> list[dict]:
        return json.loads((Path(self.paths.style) / "episodes.json").read_text(encoding="utf-8"))

    def verses(self) -> dict[str, dict]:
        p = Path(self.paths.verses)
        if not p.exists():
            return {}
        return {f"{v['chapter']}.{v['verse']}": v for v in json.loads(p.read_text(encoding="utf-8"))}


def load(path: str | Path = "reels.yaml") -> Settings:
    with open(path, encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f))
