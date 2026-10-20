"""Cinematography presets and the prompt composer.

A Shot (writer.py) names a lens, camera move, lighting setup and focus strategy by preset id. The composer
expands those ids into explicit language in a fixed order, then a provider style decides which sections a model
gets and how they are phrased. Hosted video models interpret this text probabilistically: the camera path is a
request, not something the pipeline can enforce, so visual QC checks the result afterwards.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from .settings import Settings
from .writer import ReelPlan, Shot

# Fallbacks when the planner names a preset that does not exist, chosen per shot purpose.
PURPOSE_DEFAULTS = {
    "hero_closeup": ("portrait_85", "push_in", "golden_backlight", "eyes"),
    "wide": ("wide_32", "track_lateral", "golden_backlight", "deep"),
    "macro_insert": ("macro_100", "locked_off", "golden_backlight", "rack_to_subject"),
    "low_angle_detail": ("wide_32", "ground_track", "golden_side", "follow"),
    "prop_detail": ("normal_50", "orbit_subtle", "golden_side", "eyes"),
    "establishing": ("tele_120", "crane_up", "golden_backlight", "deep"),
    "reveal": ("wide_32", "low_heroic_reveal", "volumetric_rays", "deep"),
}
HEIGHTS = {"ground": "camera just above the ground", "low": "low camera looking up, below the subject's eyeline",
           "eye": "camera at eye level", "high": "camera above the subject looking down",
           "overhead": "camera high overhead looking down"}
ANIMALS = re.compile(r"\b(horse|horses|elephant|cow|calf|deer|bird|peacock|parrot|bee|serpent|snake|tiger|lion|"
                     r"monkey|hanuman|swan|eagle|garuda|animal|creature)\b", re.I)
PEOPLE = re.compile(r"\b(man|woman|warrior|king|queen|prince|face|hands?|krishna|arjuna|sanjaya|figure|people|"
                    r"army|soldier|charioteer|sage|child|person)\b", re.I)
SECTIONS = ["subject", "environment", "composition", "camera_angle", "lens", "focus", "camera_move",
            "subject_motion", "lighting", "atmosphere", "materials", "grade", "temporal", "end_frame", "quality"]


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def presets(s: Settings) -> dict:
    p = Path(s.paths.style) / "cinematography.yaml"
    return _load(str(p), p.stat().st_mtime)


def presets_menu(s: Settings) -> str:
    c = presets(s)
    lines = ["lens: " + "; ".join(f"{k} ({v['focal']}, {v['use']})" for k, v in c["lenses"].items()),
             "move: " + "; ".join(f"{k} ({v['label']}: {v['path']})" for k, v in c["camera_moves"].items()),
             "lighting: " + "; ".join(f"{k} ({v['label']})" for k, v in c["lighting"].items()),
             "focus: " + "; ".join(f"{k} ({v})" for k, v in c["focus"].items())]
    return "\n".join(lines)


def normalize(shot: Shot, s: Settings) -> list[str]:
    """Replace unknown preset ids with the purpose default. Returns notes about what changed."""
    c, notes = presets(s), []
    lens, move, light, focus = PURPOSE_DEFAULTS[shot.purpose]
    for field, table, default in (("lens", "lenses", lens), ("move", "camera_moves", move),
                                  ("lighting", "lighting", light), ("focus", "focus", focus)):
        if getattr(shot, field) not in c[table]:
            notes.append(f"{field} '{getattr(shot, field)}' is not a preset; used '{default}'")
            setattr(shot, field, default)
    return notes


def lint(plan: ReelPlan) -> list[str]:
    """Deterministic cinematography checks; fixes the hard rules, reports the rest."""
    notes = []
    wides = [i for i, sh in enumerate(plan.shots) if sh.lens == "ultrawide_21"]
    for i in wides[1:]:
        plan.shots[i].lens = "wide_32"
        notes.append(f"shot {i + 1}: second ultrawide replaced with wide_32")
    for i in range(1, len(plan.shots)):
        a, b = plan.shots[i - 1], plan.shots[i]
        if (a.lens, a.move, a.purpose) == (b.lens, b.move, b.purpose):
            notes.append(f"shots {i} and {i + 1} repeat the same lens, move and framing")
        dirs = {a.screen_direction, b.screen_direction}
        if dirs == {"left_to_right", "right_to_left"} and b.transition_in != "flash":
            notes.append(f"shots {i} and {i + 1} reverse screen direction")
    if len({sh.lens for sh in plan.shots}) < 3 and len(plan.shots) >= 5:
        notes.append("fewer than three different lenses in the whole reel")
    return notes


# --- composition ----------------------------------------------------------------------------
def sections(shot: Shot, s: Settings, characters: dict[str, str] | None = None) -> dict[str, str]:
    """The full shot description in the canonical order (SECTIONS)."""
    c, bible = presets(s), s.style_bible()
    chars = characters if characters is not None else s.characters()
    lens, move = c["lenses"][shot.lens], c["camera_moves"][shot.move]
    light, focus = c["lighting"][shot.lighting], c["focus"][shot.focus]
    who = "; ".join(f"{chars[k]}" for k in shot.characters if k in chars)
    frame = "vertical 9:16 frame" if (shot.framing == "vertical" and s.reel.height > s.reel.width) else "widescreen frame"
    return {
        "subject": shot.subject + (f". Identity: {who}" if who else ""),
        "environment": shot.environment,
        "composition": f"{shot.purpose.replace('_', ' ')} composed for a {frame}: {shot.composition}",
        "camera_angle": HEIGHTS[shot.camera_height],
        "lens": f"{lens['focal']} lens: {lens['look']}",
        "focus": f"depth of field {lens['aperture']}, {focus}; a deliberate focal hierarchy, not blur everywhere",
        "camera_move": (f"{move['label']}: {move['path']}, from {move['start']} to {move['end']}, {move['speed']}, "
                        f"{move['ease']}; {shot.move_detail}. Keep {move['keep']}. No {move['avoid']}"),
        "subject_motion": shot.subject_motion,
        "lighting": f"{light['key']}; {light['quality']}; {light['fill']}",
        "atmosphere": ", ".join(x for x in (shot.atmosphere, light["atmosphere"]) if x and x != "clear"),
        "materials": f"richly detailed {shot.materials}",
        "grade": (f"grade: {bible.get('look', '')}; exposure {bible.get('exposure', 'filmic')}; warm amber "
                  f"highlights, earthy midtones, controlled deep shadows, natural skin, never uniformly orange"),
        "temporal": c["quality"]["positive"],
        "end_frame": f"the shot settles on: {shot.end_frame}",
        "quality": c["camera_body"],
    }


def negative(shot: Shot, s: Settings) -> str:
    q = presets(s)["quality"]["negative"]
    text = f"{shot.subject} {shot.subject_motion} {' '.join(shot.characters)}"
    parts = [q["always"]]
    if shot.characters or PEOPLE.search(text):
        parts.append(q["people"])
    if ANIMALS.search(text):
        parts.append(q["animals"])
    neg = ", ".join(" ".join(p.split()) for p in parts)
    if shot.move == "handheld_stabilized":
        neg = neg.replace("random camera shake, ", "")
    return neg


def _join(sec: dict[str, str], keys: list[str], labelled: bool) -> str:
    items = [(k, sec[k]) for k in keys if sec.get(k)]
    if labelled:
        return "\n".join(f"{k.replace('_', ' ').title()}: {v}." for k, v in items)
    return " ".join(v.rstrip(".") + "." for _, v in items)


def keyframe_prompt(shot: Shot, s: Settings, hint: str = "") -> str:
    """The opening still for image-to-video: the move's start framing, no motion language."""
    sec = sections(shot, s)
    start = presets(s)["camera_moves"][shot.move]["start"]
    sec["composition"] += f". This is the first frame of the shot, framed as a {start}"
    body = _join(sec, ["subject", "environment", "composition", "camera_angle", "lens", "focus", "lighting",
                       "atmosphere", "materials", "grade", "quality"], labelled=False)
    return f"Photoreal cinematic film still. {body} {hint}".strip()


def video_prompt(shot: Shot, s: Settings, style: str = "veo", image_to_video: bool = True, hint: str = "",
                 max_chars: int = 0) -> str:
    """Model-specific motion prompt. With image-to-video the image already fixes the look, so the text leads
    with what moves: the camera first, then the world."""
    sec = sections(shot, s)
    if image_to_video:
        keys = ["camera_move", "subject_motion", "atmosphere", "lighting", "temporal", "end_frame"]
        sec["lighting"] = "lighting and shadows stay consistent with the first frame: " + sec["lighting"]
    else:
        keys = list(SECTIONS)
    if style == "kling":       # shorter, comma-led prompts
        keys = [k for k in keys if k not in ("quality", "grade")]
    text = _join(sec, keys, labelled=(style == "generic")) + (f" {hint}" if hint else "")
    while max_chars and len(text) > max_chars and len(keys) > 2:
        keys.pop(-2)           # drop the least important trailing detail, keep end_frame
        text = _join(sec, keys, labelled=(style == "generic")) + (f" {hint}" if hint else "")
    return text[:max_chars] if max_chars else text
