"""Footage: character sheets, then one *take* per call: hidden keyframe -> QC -> image-to-video -> QC.

Keyframes are inputs to the video model only; they never appear in the edit. Each take lives in its own
folder (shots/NN/tKK/) with a take.json, so regenerating a shot never touches earlier or approved takes.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from .cinema import keyframe_prompt, negative, video_prompt
from .fal import BudgetExceeded, Fal
from .providers import Animatic, VideoRequest
from .settings import Settings
from .writer import Claude, Shot, qc_passes

log = logging.getLogger(__name__)


class ShotFailed(RuntimeError):
    pass


@dataclass
class ShotResult:
    index: int
    clip: Path
    keyframe: Path
    model: str
    gen_seconds: int
    attempts: int
    qc: list[dict] = field(default_factory=list)


def refs_dir(s: Settings) -> Path:
    """Character sheets are shared across reels; a concept project with its own cast keeps its own."""
    return Path(s.paths.characters).parent / "refs" if s.paths.characters else Path(s.paths.style) / "refs"


def ensure_refs(s: Settings, fal: Fal, ids: set[str]) -> dict[str, Path]:
    """Create a character reference sheet once per character; reused by every reel after that."""
    chars, out = s.characters(), {}
    look = s.style_bible()["look"]
    for cid in sorted(ids):
        if cid not in chars:
            continue
        path = refs_dir(s) / f"{cid}.png"
        if not path.exists():
            body = {"prompt": f"Character reference sheet, three views side by side (front, three-quarter, "
                              f"profile) on a plain neutral background, full body. {chars[cid]}. Style: {look}. "
                              f"No text, no labels.", "num_images": 1, "aspect_ratio": "16:9"}
            res = fal.run(s.fal.refs_model, body, s.fal.keyframe_price)
            fal.download(res["images"][0]["url"], path)
            log.info("created reference sheet %s", path)
        out[cid] = path
    return out


def frames_of(clip: Path, folder: Path, n: int = 4) -> list[Path]:
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(clip)], capture_output=True, text=True, check=True).stdout)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for k in range(n):
        p = folder / f"{clip.stem}_qc{k}.jpg"
        t = dur * (k + 0.5) / n
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(clip), "-frames:v", "1",
                        "-vf", "scale=-2:768", str(p)], check=True)
        paths.append(p)
    return paths


def motion_metrics(clip: Path, samples: int = 12) -> dict:
    """Cheap temporal checks on a clip: does it actually move, and does brightness flicker?

    motion: mean absolute frame difference (0-255) between evenly spaced frames
    flicker: largest jump in mean brightness between neighbouring samples, relative to the median jump
    """
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(clip), "-vf", "scale=96:-2,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    w = 96
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(clip)], capture_output=True, text=True, check=True).stdout
    sw, sh = (int(x) for x in probe.strip().split(",")[:2])
    h = int(round(sh * w / sw / 2) * 2)
    frames = np.frombuffer(raw, np.uint8)[: (len(raw) // (w * h)) * w * h].reshape(-1, h, w).astype(np.float32)
    if len(frames) < 3:
        return {"frames": int(len(frames)), "motion": 0.0, "flicker": 0.0, "notes": ["clip too short to measure"]}
    idx = np.linspace(0, len(frames) - 1, min(samples, len(frames))).astype(int)
    pick = frames[idx]
    diffs = np.abs(np.diff(pick, axis=0)).mean(axis=(1, 2))
    means = pick.mean(axis=(1, 2))
    jumps = np.abs(np.diff(means))
    flicker = float(jumps.max() / (np.median(jumps) + 0.5))
    notes = []
    if diffs.mean() < 0.6:
        notes.append("almost no movement: the model may have returned a near-still")
    if flicker > 6 and jumps.max() > 6:
        notes.append("sudden brightness jump: possible flicker or lighting pop")
    return {"frames": int(len(frames)), "motion": round(float(diffs.mean()), 2), "flicker": round(flicker, 2),
            "notes": notes}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TakeMaker:
    """Makes one take of one shot. No retries here: the caller decides whether another take is worth it."""

    def __init__(self, s: Settings, keyframer, claude: Claude | None):
        self.s, self.keyframer, self.claude = s, keyframer, claude

    def make(self, index: int, shot: Shot, folder: Path, providers: list, refs: dict[str, Path],
             hint: str = "", overrides: dict | None = None, seed: int | None = None) -> dict:
        overrides = overrides or {}
        folder.mkdir(parents=True, exist_ok=True)
        r = self.s.reel
        aspect = "16:9" if shot.framing == "rotated_wide" else (
            "9:16" if r.height > r.width else "1:1" if r.height == r.width else "16:9")
        shot_refs = [refs[c] for c in shot.characters if c in refs]
        providers = route(providers, shot.model)
        primary = providers[0]
        # A hand-written prompt (prompt_overrides) still gets the reviewer's retake note.
        keyframe = (f"{overrides['keyframe']} {hint}".strip() if overrides.get("keyframe")
                    else keyframe_prompt(shot, self.s, hint))
        take = {"id": folder.name, "shot": index, "created": _now(), "status": "generating", "hint": hint,
                "provider": primary.name, "placeholder": bool(getattr(primary.caps, "placeholder", False)),
                "prompts": {"keyframe": keyframe,
                            "negative": overrides.get("negative") or negative(shot, self.s)},
                "spec": shot.model_dump(), "qc": [], "cost_usd": 0.0}
        save_take(folder, take)
        try:
            # 1. hidden keyframe, checked before paying for video
            kpath, kurl, cost = self.keyframer.generate(take["prompts"]["keyframe"], aspect, shot_refs,
                                                        folder / "keyframe.png")
            take.update(keyframe=kpath.name, cost_usd=take["cost_usd"] + cost)
            if self.claude and not self.keyframer.placeholder:
                q = self.claude.qc([kpath], shot_refs, shot)
                take["qc"].append({"stage": "keyframe", **q.model_dump()})
                if not qc_passes(q, self.s.claude.min_qc_score):
                    take.update(status="failed_qc", retry_hint=q.retry_hint, ended=_now())
                    return save_take(folder, take)
            # 2. motion, trying each provider in turn if one errors
            errors, result = [], None
            for p in providers:
                if overrides.get("video"):      # hand-written: sent as written, plus the retake note
                    vprompt = f"{overrides['video']} {hint}".strip()
                else:
                    vprompt = with_sound_and_avoid(
                        video_prompt(shot, self.s, style=p.style, hint=hint, max_chars=p.caps.max_prompt_chars),
                        shot, take["prompts"]["negative"], p.caps)
                if isinstance(p, Animatic):
                    p.move = shot.move
                req = VideoRequest(prompt=vprompt, negative=take["prompts"]["negative"],
                                   seconds=shot.on_screen_seconds + 0.8, aspect=aspect, out=folder / "clip.mp4",
                                   image_url=kurl, image_path=kpath, seed=seed)
                try:
                    result = p.generate(req)
                    take["prompts"]["video"] = vprompt
                    break
                except BudgetExceeded:
                    raise
                except Exception as e:  # model down or rejected input: fall back to the next provider
                    log.warning("video provider %s failed for shot %s: %s", p.name, index + 1, e)
                    errors.append(f"{p.name}: {e}")
            if result is None:
                take.update(status="failed", error="every video provider failed: " + " | ".join(errors), ended=_now())
                return save_take(folder, take)
            take.update(clip=result.path.name, provider=result.provider, model=result.model,
                        gen_seconds=result.gen_seconds, request=result.body, placeholder=result.placeholder,
                        cost_usd=round(take["cost_usd"] + result.cost_usd, 4), fallback_errors=errors)
            take["metrics"] = motion_metrics(result.path)
            # 3. visual QC on frames from across the whole clip
            if self.claude and not result.placeholder:
                q = self.claude.qc(frames_of(result.path, folder / "qc"), shot_refs, shot)
                take["qc"].append({"stage": "clip", **q.model_dump()})
                if not qc_passes(q, self.s.claude.min_qc_score):
                    take.update(status="failed_qc", retry_hint=q.retry_hint, ended=_now())
                    return save_take(folder, take)
            take.update(status="ready", ended=_now(), reviewed=bool(self.claude and not result.placeholder))
            return save_take(folder, take)
        except BudgetExceeded as e:
            take.update(status="failed", error=str(e), ended=_now())
            save_take(folder, take)
            raise
        except Exception as e:
            take.update(status="failed", error=f"{type(e).__name__}: {e}", ended=_now())
            save_take(folder, take)
            raise


def route(providers: list, model: str) -> list:
    """The provider chain for one shot: the shot's named model first, then every model that is in the default
    chain (models with chain: false only run for shots that name them)."""
    named = [p for p in providers if model and p.name == model]
    if model and not named:
        raise ValueError(f"shot names model {model!r}, which is not configured in fal.video_models")
    return named + [p for p in providers if getattr(p, "chain", True) and p not in named]


def with_sound_and_avoid(prompt: str, shot: Shot, negative_text: str, caps) -> str:
    """Models that make their own sound hear the shot's sound list (and no score: music is added in the edit).
    Models without a negative_prompt field get the negative list as an 'Avoid' sentence."""
    if caps.placeholder:
        return prompt
    if caps.native_audio:
        prompt += f" Sound: {shot.sfx or 'natural ambience of the scene'}; no music, no speech, no singing."
    if negative_text and not caps.negative_prompt and "Avoid:" not in prompt:
        prompt += f" Avoid: {negative_text}."
    return prompt


def save_take(folder: Path, take: dict) -> dict:
    tmp = folder / "take.json.tmp"
    tmp.write_text(json.dumps(take, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(folder / "take.json")
    return take
