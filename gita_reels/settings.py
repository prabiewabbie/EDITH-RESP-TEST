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
    codec: Literal["h264", "h265"] = "h264"
    grade: str = "reference_warm"  # grade preset id from style/cinematography.yaml
    match_shots: bool = True       # nudge each shot's colour balance toward the reel median before grading


class ClaudeSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort_write: str = "high"
    effort_review: str = "high"
    effort_qc: str = "medium"
    min_review_score: int = 8
    min_qc_score: int = 7


class VideoModel(BaseModel):
    id: str
    price_per_second: float = 0.0
    price_per_video: float = 0.0   # for models billed per clip; used instead of price_per_second when set
    durations: list[int] = [4, 6, 8]
    duration_format: str = "{n}s"
    resolution: str = "720p"
    native_audio: bool = False
    # How this model wants its prompt (cinema.video_prompt): "veo" prose, "kling" short, "generic" labelled lines.
    style: Literal["veo", "kling", "generic"] = "veo"
    negative_prompt: bool = False  # send negative_prompt only to models whose schema has it (check-models)
    seed_field: str = ""           # e.g. "seed" when the model accepts one
    aspect_ratios: list[str] = ["9:16", "16:9"]
    max_prompt_chars: int = 0      # 0 = no limit
    extra: dict = {}               # any other documented input fields, passed through as-is
    omit: list[str] = []           # standard fields this model does not take, e.g. ["duration", "resolution"]
    chain: bool = True             # false = used only for shots that name it in their `model` field
    note: str = ""

    def pick_duration(self, wanted: float) -> int:
        return next((d for d in sorted(self.durations) if d >= wanted), max(self.durations))

    def cost(self, wanted: float) -> float:
        return self.price_per_video or self.pick_duration(wanted) * self.price_per_second


class FalSettings(BaseModel):
    keyframe_model: str
    keyframe_refs_field: str = "image_urls"
    refs_model: str
    video_models: list[VideoModel]
    keyframe_price: float = 0.08
    # Scored reels (a plan with a cue_sheet) get a music bed from this model, shaped to the cue sheet;
    # empty = the synthesised score in score.py.
    music_model: str = ""
    music_price: float = 0.6
    daily_budget_usd: float = 15
    reel_budget_usd: float = 8
    max_retries_per_shot: int = 2
    poll_seconds: float = 5
    timeout_seconds: float = 900


class LocalSettings(BaseModel):
    """Free generation on your own machine; see providers.LocalCommand for the placeholders."""
    video_command: str = ""
    image_command: str = ""        # optional; without it, local video takes start from an animatic keyframe
    durations: list[int] = [3, 4, 5]
    max_prompt_chars: int = 0
    timeout_seconds: float = 3600


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
    queue: str = "queue"         # reviewed plans written by the scheduled Claude Code task, one per episode
    characters: str = ""         # a project's own character list (concept mode); default style/characters.json


class Settings(BaseModel):
    channel: Channel
    reel: ReelSettings = ReelSettings()
    claude: ClaudeSettings = ClaudeSettings()
    fal: FalSettings
    publish: PublishSettings = PublishSettings()
    local: LocalSettings = LocalSettings()
    paths: Paths = Paths()

    def style_bible(self) -> dict:
        return json.loads((Path(self.paths.style) / "style_bible.json").read_text(encoding="utf-8"))

    def characters(self) -> dict[str, str]:
        p = Path(self.paths.characters) if self.paths.characters else Path(self.paths.style) / "characters.json"
        return json.loads(p.read_text(encoding="utf-8"))

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
