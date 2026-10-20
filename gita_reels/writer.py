"""Claude stages: plan a reel, review it for scripture fidelity, and visually QC generated frames."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .settings import Settings

GUARDRAILS = """\
- The Bhagavad Gita is sacred to about a billion people. Every frame must be reverent and dignified.
- The episode synopsis given to you is the story. Do not add events, dialogue or claims that are not in it.
- The only words that may appear as scripture are the provided verse translation, quoted exactly.
- Depict Krishna and all figures in traditional iconography; nothing sexualised, comedic, meme-like or gory.
- Battle stays at a distance; no blood, wounds or killing on screen. No real people or celebrity likenesses."""


class Shot(BaseModel):
    """One production unit. Preset ids come from style/cinematography.yaml; cinema.py expands them."""
    purpose: Literal["hero_closeup", "wide", "macro_insert", "low_angle_detail", "prop_detail",
                     "establishing", "reveal"]
    characters: list[str] = Field(description="Character ids from the character list that appear; [] for none")
    subject: str = Field(description="Who or what is in frame and their exact pose, expression and wardrobe "
                                     "details for this moment (identity comes from the character list)")
    environment: str = Field(description="Where we are: setting, era, time of day, what surrounds the subject")
    composition: str = Field(description="Exact framing and screen position, plus the foreground, middle-ground "
                                         "and background layers and any negative space")
    camera_height: Literal["ground", "low", "eye", "high", "overhead"]
    lens: str = Field(description="Lens preset id")
    focus: str = Field(description="Focus preset id")
    move: str = Field(description="Camera move preset id")
    move_detail: str = Field(description="What the camera does in this shot: start framing -> end framing, "
                                         "direction, speed. One move only")
    subject_motion: str = Field(description="What moves in the world during the shot, starting with a strong "
                                            "motion verb; plausible weight and contact")
    lighting: str = Field(description="Lighting preset id")
    atmosphere: str = Field(description="Haze, dust, smoke, petals, wind; or 'clear'")
    materials: str = Field(description="Key surface textures to render: silk, gold, bronze, skin, fur, stone")
    end_frame: str = Field(description="The final frame the shot settles on and how it hands over to the next")
    screen_direction: Literal["left_to_right", "right_to_left", "toward_camera", "away", "none"] = Field(
        description="Main direction of movement or gaze; keep it consistent between neighbouring shots")
    on_screen_seconds: float = Field(description="How long the shot stays on screen, 1.2-4.0 s")
    framing: Literal["vertical", "rotated_wide"] = Field(
        description="rotated_wide = widescreen shot laid sideways in the vertical frame, at most once per reel")
    transition_in: Literal["cut", "match_cut", "flash"] = Field(
        description="match_cut = cut on matching shape, gaze or motion; flash = quick dip from white, big reveal")
    sfx: str = Field(description="Sound cues synced to visible action, e.g. 'hooves on dirt, wind'; or empty")
    narration: str = Field(description="Voice-over line for this shot, or empty")


class ReelPlan(BaseModel):
    title: str = Field(description="YouTube title under 70 characters, no hashtags")
    description: str = Field(description="2-3 sentence description naming the chapter and verse")
    tags: list[str] = Field(description="8-15 tags, no # symbol")
    music_mood: Literal["epic", "devotional", "calm", "tension", "triumphant"]
    shots: list[Shot]
    verse_shot_index: int = Field(description="Index of the shot that carries the verse line, or -1")
    verse_line: str = Field(description="A short exact excerpt of the verse translation, or empty")


class Issue(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    problem: str
    fix: str


class PlanReview(BaseModel):
    fidelity_score: int = Field(description="0-10 faithfulness to the episode synopsis and the verse")
    respect_score: int = Field(description="0-10 reverence of every shot description")
    craft_score: int = Field(description="0-10 cinematography: varied deliberate shots, exact camera moves, "
                                         "depth layering, continuity")
    issues: list[Issue]
    verdict: Literal["approve", "revise", "reject"]


class VisualQC(BaseModel):
    identity: int = Field(description="0-10 characters match their reference sheets (10 if no characters)")
    anatomy: int = Field(description="0-10 hands, faces, limbs and animals look correct")
    artifacts: int = Field(description="0-10 free of warping, flicker, melted objects, stray text or logos")
    respect: int = Field(description="0-10 dignified, traditional depiction; nothing disrespectful")
    prompt_match: int = Field(description="0-10 matches the shot description and style")
    camera_motion: int = Field(description="0-10 the camera move reads as the specified physical move (not a "
                                           "digital zoom), smooth, with believable parallax (10 for a keyframe)")
    lighting_continuity: int = Field(description="0-10 light direction, colour and shadows stay consistent")
    problems: list[str] = Field(description="Actionable notes, e.g. 'the face changes shape midway', "
                                            "'feet slide across the ground', 'reads as a digital zoom'")
    retry_hint: str = Field(description="One sentence to add to the prompt to fix the problems, or empty")


class Claude:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.s = settings
        self._client = client
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    @property
    def client(self) -> anthropic.Anthropic:
        self._client = self._client or anthropic.Anthropic(max_retries=4)
        return self._client

    def ask(self, system: str, content: str | list, schema: type[BaseModel], effort: str) -> BaseModel:
        resp = self.client.beta.messages.parse(
            model=self.s.claude.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": effort},
            fallbacks="default",
            betas=["server-side-fallback-2026-07-01"],
        )
        self.usage["calls"] += 1
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        if resp.parsed_output is None:
            raise RuntimeError(f"no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    def cost_usd(self) -> float:  # Claude Opus 5.5 list price: $4 / $20 per million tokens
        return (self.usage["input_tokens"] * 4 + self.usage["output_tokens"] * 20) / 1_000_000

    # --- planning -----------------------------------------------------------------------
    def plan(self, episode: dict, verse: dict | None, feedback: PlanReview | None = None,
             direction: str = "") -> ReelPlan:
        system, prompt = self.plan_request(episode, verse, feedback, direction)
        return self.ask(system, prompt, ReelPlan, self.s.claude.effort_write)

    def plan_request(self, episode: dict, verse: dict | None, feedback: PlanReview | None = None,
                     direction: str = "") -> tuple[str, str]:
        """The planner's system prompt and request, also exported by `gita-reels brief` for planning in a
        Claude chat without an API key."""
        from .cinema import presets_menu
        bible, chars, r = self.s.style_bible(), self.s.characters(), self.s.reel
        concept = episode.get("kind") == "concept"
        subject = ("an original cinematic sequence from the brief below" if concept else
                   "the Bhagavad Gita in order, one moment per reel")
        system = (
            f"You are the director and cinematographer of '{self.s.channel.name}', a channel of fully animated, "
            f"photoreal cinematic reels that tell {subject}. Every frame should feel lifted from a high-budget "
            f"live-action historical epic, not a generic AI video.\n"
            f"Match this measured reference style:\n{json.dumps(bible, indent=1)}\n"
            f"Cinematography presets (use these ids for lens, move, lighting and focus):\n{presets_menu(self.s)}\n"
            f"Rules: {r.min_seconds:.0f}-{r.max_seconds:.0f} seconds total; 6-12 shots; shot lengths close to "
            f"avg_shot_seconds, holding close-ups long enough to register and keeping details brief; alternate "
            f"intimate detail and expansive spectacle following shot_scale_pattern; never repeat the same lens, "
            f"move and distance on neighbouring shots; ultrawide_21 at most once; open on motion within the first "
            f"second; one camera move per shot, described exactly (start and end framing, direction, speed) and "
            f"kept slow and deliberate; give every shot foreground, middle-ground and background layers where the "
            f"setting allows; keep screen direction, eyelines and light direction continuous between shots unless a "
            f"reversal is the point; hard or match cuts on the beat; at most one flash transition; end on a "
            f"composed final image that is allowed to breathe"
            + ("; at most one rotated_wide shot" if r.rotated_wide_shots and r.height > r.width
               else "; never use rotated_wide")
            + ("; write a short narration line for some shots" if r.narration else "; leave narration empty")
            + ".\nEach shot is generated separately, so every subject field must describe the characters fully "
              f"using the character list.\n{GUARDRAILS}")
        if concept:
            prompt = (f"Brief: {episode['story']}\nMain character: {episode.get('character') or 'from the brief'}\n"
                      f"Environment: {episode.get('environment') or 'from the brief'}\n")
        else:
            prompt = (f"Episode {episode['id']} (chapter {episode['chapter']}): {episode['title']}\n"
                      f"Synopsis: {episode['story']}\nSet pieces to draw on: {'; '.join(episode['set_pieces'])}\n")
        prompt += f"Characters: {json.dumps(chars, indent=1)}\n"
        if direction:
            prompt += f"Director's notes from the user (follow them): {direction}\n"
        if verse and r.verse_line:
            prompt += (f"Verse BG {verse['chapter']}.{verse['verse']} translation (excerpt it exactly, max 14 words):\n"
                       f"{verse['translation']}\n")
        else:
            prompt += "No verse text available: set verse_shot_index to -1 and verse_line to empty.\n"
        if feedback:
            prompt += "\nThe reviewer asked for changes. Fix every issue:\n" + "\n".join(
                f"- [{i.severity}] {i.problem} -> {i.fix}" for i in feedback.issues)
        return system, prompt

    def review(self, plan: ReelPlan, episode: dict, verse: dict | None) -> PlanReview:
        system = ("You are a strict reviewer for scripture fidelity and respectful depiction, and a demanding "
                  "director of photography. You did not write this plan. Flag invented events or dialogue, anything "
                  "irreverent, and cinematography that would read as generic AI video (same framing every shot, "
                  "vague camera moves, broken screen direction). Approve only with no blocker or major issues.\n"
                  + GUARDRAILS)
        prompt = (f"{'Brief' if episode.get('kind') == 'concept' else 'Episode synopsis'} (ground truth): "
                  f"{episode['story']}\n"
                  + (f"Verse translation: {verse['translation']}\n" if verse else "No verse text.\n")
                  + f"Plan:\n{plan.model_dump_json(indent=1)}")
        return self.ask(system, prompt, PlanReview, self.s.claude.effort_review)

    # --- visual QC ----------------------------------------------------------------------
    def qc(self, frames: list[Path], refs: list[Path], shot: Shot) -> VisualQC:
        content: list = []
        for label, paths in (("Character reference sheets:", refs), ("Frames from the generated shot:", frames)):
            if not paths:
                continue
            content.append({"type": "text", "text": label})
            content += [_image_block(p) for p in paths]
        content.append({"type": "text", "text": f"Shot description:\n{shot.model_dump_json(indent=1)}\n"
                                                "Score the generated frames."})
        system = ("You are a demanding VFX supervisor checking AI-generated footage for a devotional channel "
                  "before it is published. Be strict about faces, hands, identity drift and anything "
                  "disrespectful.\n" + GUARDRAILS)
        return self.ask(system, content, VisualQC, self.s.claude.effort_qc)


def qc_passes(q: VisualQC, minimum: int) -> bool:
    return min(q.identity, q.anatomy, q.artifacts, q.respect, q.prompt_match, q.camera_motion,
               q.lighting_continuity) >= minimum


def _image_block(path: Path) -> dict:
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(path.read_bytes()).decode()}}
