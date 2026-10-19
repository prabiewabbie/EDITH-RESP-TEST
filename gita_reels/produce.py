"""Footage: character sheets, a hidden keyframe per shot, image-to-video, and visual QC with retries.

Keyframes are inputs to the video model only; they never appear in the edit.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .fal import BudgetExceeded, Fal, data_uri
from .settings import Settings
from .writer import Claude, ReelPlan, Shot, qc_passes

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
    return Path(s.paths.style) / "refs"


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


def frames_of(clip: Path, folder: Path, n: int = 3) -> list[Path]:
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


class Producer:
    def __init__(self, s: Settings, fal: Fal, claude: Claude):
        self.s, self.fal, self.claude = s, fal, claude

    def estimate(self, plan: ReelPlan, retry_factor: float = 1.3) -> float:
        vm = self.s.fal.video_models[0]
        video = sum(vm.pick_duration(sh.on_screen_seconds + 0.8) for sh in plan.shots) * vm.price_per_second
        return round((len(plan.shots) * self.s.fal.keyframe_price + video) * retry_factor, 2)

    def produce(self, plan: ReelPlan, folder: Path) -> list[ShotResult]:
        self.fal.start_reel()
        refs = ensure_refs(self.s, self.fal, {c for sh in plan.shots for c in sh.characters})
        return [self._shot(i, sh, refs, folder) for i, sh in enumerate(plan.shots)]

    def _shot(self, i: int, shot: Shot, refs: dict[str, Path], folder: Path) -> ShotResult:
        bible, chars = self.s.style_bible(), self.s.characters()
        shot_refs = [refs[c] for c in shot.characters if c in refs]
        aspect = "16:9" if shot.framing == "rotated_wide" else "9:16"
        described = "; ".join(f"{c}: {chars[c]}" for c in shot.characters if c in chars)
        hint, qc_log = "", []
        for attempt in range(1, self.s.fal.max_retries_per_shot + 2):
            # 1. hidden keyframe, checked before paying for video
            kprompt = (f"{shot.keyframe_prompt}. {described}. Style: {bible['look']}. Camera framing: "
                       f"{shot.purpose.replace('_', ' ')}. {hint} Avoid: {bible['negative_prompt']}")
            body = {"prompt": kprompt, "num_images": 1, "aspect_ratio": aspect}
            if shot_refs:
                body[self.s.fal.keyframe_refs_field] = [data_uri(p) for p in shot_refs]
            model = self.s.fal.keyframe_model if shot_refs else self.s.fal.refs_model
            kres = self.fal.run(model, body, self.s.fal.keyframe_price)
            kurl = kres["images"][0]["url"]
            keyframe = self.fal.download(kurl, folder / "keyframes" / f"shot_{i + 1:02d}_try{attempt}.png")
            q = self.claude.qc([keyframe], shot_refs, shot)
            qc_log.append({"stage": "keyframe", "attempt": attempt, **q.model_dump()})
            if not qc_passes(q, self.s.claude.min_qc_score):
                hint = q.retry_hint
                continue
            # 2. motion, trying each configured video model in turn
            clip, used, gen = self._video(i, shot, kurl, aspect, folder, attempt, hint)
            q = self.claude.qc(frames_of(clip, folder / "qc"), shot_refs, shot)
            qc_log.append({"stage": "clip", "attempt": attempt, **q.model_dump()})
            if qc_passes(q, self.s.claude.min_qc_score):
                return ShotResult(i, clip, keyframe, used, gen, attempt, qc_log)
            hint = q.retry_hint
        raise ShotFailed(f"shot {i + 1} failed QC after {attempt} attempts: {qc_log[-1]['problems']}")

    def _video(self, i: int, shot: Shot, image_url: str, aspect: str, folder: Path, attempt: int,
               hint: str) -> tuple[Path, str, int]:
        bible = self.s.style_bible()
        errors = []
        for vm in self.s.fal.video_models:
            gen = vm.pick_duration(shot.on_screen_seconds + 0.8)
            body = {"prompt": f"{shot.motion_prompt}. Camera: {shot.camera}. {bible['motion']}. {hint}",
                    "image_url": image_url, "duration": vm.duration_format.format(n=gen),
                    "resolution": vm.resolution, "aspect_ratio": aspect,
                    "negative_prompt": bible["negative_prompt"]}
            if vm.native_audio:
                body["generate_audio"] = True
            try:
                res = self.fal.run(vm.id, body, gen * vm.price_per_second)
            except BudgetExceeded:
                raise
            except Exception as e:  # model down or rejected input: fall back to the next model
                log.warning("video model %s failed for shot %s: %s", vm.id, i + 1, e)
                errors.append(f"{vm.id}: {e}")
                continue
            clip = self.fal.download(res["video"]["url"], folder / "clips_raw" / f"shot_{i + 1:02d}_try{attempt}.mp4")
            return clip, vm.id, gen
        raise ShotFailed(f"shot {i + 1}: every video model failed: {errors}")
