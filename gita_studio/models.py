"""Typed outputs for every pipeline stage. These schemas are sent to Claude as structured-output formats."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Format = Literal["short_video", "carousel", "quote_card"]


# --- Strategy -------------------------------------------------------------------------

class FormatShare(BaseModel):
    format: Format
    percent: int


class StrategyBrief(BaseModel):
    summary: str = Field(description="What the analytics say, in plain words")
    double_down: list[str] = Field(description="Themes, hooks or formats to do more of")
    stop_doing: list[str] = Field(description="Things that underperformed")
    experiments: list[str] = Field(description="One or two new things to test next week")
    format_mix: list[FormatShare] = Field(description="Suggested share per format, summing to 100")


# --- Ideation -------------------------------------------------------------------------

class Idea(BaseModel):
    chapter: int
    verse: int
    format: Format
    angle: str = Field(description="The modern-life lens, e.g. 'exam anxiety', 'burnout at work'")
    hook: str = Field(description="First line / first 2 seconds that stops the scroll")
    rationale: str = Field(description="Why this verse speaks to this angle, grounded in the verse's meaning")


class IdeaBatch(BaseModel):
    ideas: list[Idea]


# --- Script ---------------------------------------------------------------------------

class Scene(BaseModel):
    seconds: float = Field(description="Duration of the scene (video) or 0 for static slides")
    narration: str = Field(description="Voice-over or on-slide text")
    on_screen_text: str
    visual: str = Field(description="What the viewer sees, described for an image/video generator")


class Script(BaseModel):
    title: str
    hook: str
    scenes: list[Scene]
    verse_quote: str = Field(description="The verse translation exactly as provided, no paraphrase")
    reflection: str = Field(description="2-4 sentence modern application")
    call_to_action: str


# --- Review ---------------------------------------------------------------------------

class Issue(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    where: str
    problem: str
    fix: str


class Review(BaseModel):
    fidelity_score: int = Field(description="0-10: faithfulness to the verse and its traditional meaning")
    respect_score: int = Field(description="0-10: reverence and adherence to depiction rules")
    quote_is_exact: bool = Field(description="verse_quote matches the provided translation verbatim")
    issues: list[Issue]
    verdict: Literal["approve", "revise", "reject"]


# --- Visuals --------------------------------------------------------------------------

class VisualAsset(BaseModel):
    kind: Literal["image", "video", "voiceover", "music"]
    scene_index: int
    prompt: str
    negative_prompt: str = ""
    aspect: str
    duration_seconds: float = 0


class VisualPlan(BaseModel):
    style_guide: str = Field(description="Shared art direction so every asset in the post looks consistent")
    assets: list[VisualAsset]


# --- Distribution ---------------------------------------------------------------------

class PlatformCopy(BaseModel):
    platform: str
    language: str
    caption: str
    hashtags: list[str]
    title: str = ""
    alt_text: str = Field(description="Accessibility description of the visual")


class Distribution(BaseModel):
    copies: list[PlatformCopy]
