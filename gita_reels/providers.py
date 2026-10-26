"""Generation providers behind one interface, with their real differences spelled out as capabilities.

- FalVideo: any fal.ai image-to-video endpoint listed under fal.video_models in reels.yaml.
- FalImage: the keyframe and character-sheet image models on fal.
- LocalCommand / LocalImageCommand: free paths. Run your own command (an open-weight model such as Wan or
  LTX-Video on your own GPU, a ComfyUI workflow script, anything) configured under `local:` in reels.yaml.
- Manual: no generation at all. You make the clip in any tool you like (free tiers included) and import it
  as a take with `gita-reels import-take` or the upload button in the studio UI.
- Animatic / AnimaticImage: local ffmpeg/Pillow placeholders for testing the pipeline and UI with no keys.
  Every frame carries an "ANIMATIC" label, takes are flagged placeholder=True, and a reel built from them
  gets status "animatic", which the publisher refuses. They are never presented as generated footage.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from .fal import Fal, data_uri
from .settings import Settings, VideoModel

PLACEHOLDER_LABEL = "ANIMATIC - placeholder, not generated footage"


class Capabilities(BaseModel):
    image_to_video: bool
    text_to_video: bool = False
    negative_prompt: bool = False
    seed: bool = False
    durations: list[int]
    aspect_ratios: list[str]
    native_audio: bool = False
    max_prompt_chars: int = 0
    price_per_second: float | None = None
    cancel: bool = False           # a submitted fal job runs to completion; stop the take's process instead
    placeholder: bool = False


@dataclass
class VideoRequest:
    prompt: str
    negative: str
    seconds: float                 # wanted on-screen length; the provider picks an allowed duration
    aspect: str
    out: Path
    image_url: str = ""            # remote URL or data: URI of the keyframe
    image_path: Path | None = None
    seed: int | None = None


@dataclass
class VideoResult:
    path: Path
    provider: str
    model: str
    gen_seconds: int
    cost_usd: float
    body: dict = field(default_factory=dict)   # what was sent, with inline images elided
    placeholder: bool = False


def _elide(body: dict) -> dict:
    def short(v):
        if isinstance(v, str) and v.startswith("data:"):
            return v[:32] + "...(inline image)"
        if isinstance(v, list):
            return [short(x) for x in v]
        return v
    return {k: short(v) for k, v in body.items()}


class FalVideo:
    def __init__(self, vm: VideoModel, fal: Callable[[], Fal]):
        self.vm, self._fal = vm, fal
        self.name, self.chain = vm.id, vm.chain
        self.caps = Capabilities(image_to_video=True, negative_prompt=vm.negative_prompt, seed=bool(vm.seed_field),
                                 durations=vm.durations, aspect_ratios=vm.aspect_ratios,
                                 native_audio=vm.native_audio, max_prompt_chars=vm.max_prompt_chars,
                                 price_per_second=vm.price_per_second)

    @property
    def style(self) -> str:
        return self.vm.style

    def validate_config(self) -> list[str]:
        issues = []
        if not self.vm.durations:
            issues.append(f"{self.name}: no allowed durations")
        if self.vm.price_per_second <= 0 and self.vm.price_per_video <= 0:
            issues.append(f"{self.name}: set price_per_second or price_per_video so the budget caps work")
        if "{n}" not in self.vm.duration_format:
            issues.append(f"{self.name}: duration_format must contain {{n}}")
        return issues

    def validate_credentials(self) -> list[str]:
        return [] if os.environ.get("FAL_KEY") else ["FAL_KEY is not set"]

    def estimate(self, seconds: float) -> float:
        return self.vm.cost(seconds)

    def body(self, req: VideoRequest) -> dict:
        gen = self.vm.pick_duration(req.seconds)
        aspect = req.aspect if req.aspect in self.caps.aspect_ratios else self.caps.aspect_ratios[0]
        body = {"prompt": req.prompt, "image_url": req.image_url or data_uri(req.image_path),
                "duration": self.vm.duration_format.format(n=gen), "resolution": self.vm.resolution,
                "aspect_ratio": aspect, **self.vm.extra}
        if self.caps.negative_prompt and req.negative:
            body["negative_prompt"] = req.negative
        if self.caps.seed and req.seed is not None:
            body[self.vm.seed_field] = req.seed
        if self.vm.native_audio:
            body["generate_audio"] = True
        for k in self.vm.omit:
            body.pop(k, None)
        return body

    def generate(self, req: VideoRequest) -> VideoResult:
        gen = self.vm.pick_duration(req.seconds)
        body = self.body(req)
        fal = self._fal()
        before = fal.reel_spent
        res = fal.run(self.vm.id, body, self.vm.cost(req.seconds))
        fal.download(res["video"]["url"], req.out)
        return VideoResult(req.out, "fal", self.vm.id, gen, round(fal.reel_spent - before, 4), _elide(body))


class FalImage:
    name = "fal-image"
    placeholder = False

    def __init__(self, s: Settings, fal: Callable[[], Fal]):
        self.s, self._fal = s, fal

    def generate(self, prompt: str, aspect: str, refs: list[Path], out: Path) -> tuple[Path, str, float]:
        """Returns (local path, URL to hand to the video model, charged USD)."""
        body = {"prompt": prompt, "num_images": 1, "aspect_ratio": aspect}
        if refs:
            body[self.s.fal.keyframe_refs_field] = [data_uri(p) for p in refs]
        model = self.s.fal.keyframe_model if refs else self.s.fal.refs_model
        fal = self._fal()
        before = fal.reel_spent
        res = fal.run(model, body, self.s.fal.keyframe_price)
        url = res["images"][0]["url"]
        fal.download(url, out)
        return out, url, round(fal.reel_spent - before, 4)


# --- local placeholders ---------------------------------------------------------------------
def _label_png(w: int, h: int, text: str, out: Path) -> Path:
    from PIL import Image, ImageDraw
    from .edit import font
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(max(18, w // 30), serif=False)
    tw = d.textlength(PLACEHOLDER_LABEL, font=f)
    d.rectangle([0, 0, w, int(h * 0.05)], fill=(160, 20, 20, 220))
    d.text(((w - tw) / 2, int(h * 0.012)), PLACEHOLDER_LABEL, font=f, fill=(255, 255, 255, 255))
    small = font(max(14, w // 42), serif=False)
    y = int(h * 0.80)
    for line in textwrap.wrap(text, width=46)[:6]:
        d.text((int(w * 0.05), y), line, font=small, fill=(255, 255, 255, 220))
        y += int(small.size * 1.3)
    img.save(out)
    return out


class AnimaticImage:
    name = "animatic-image"
    placeholder = True

    def __init__(self, s: Settings):
        self.s = s

    def generate(self, prompt: str, aspect: str, refs: list[Path], out: Path) -> tuple[Path, str, float]:
        from PIL import Image, ImageDraw
        w, h = (1280, 720) if aspect == "16:9" else (720, 1280)
        pal = [p for p in self.s.style_bible().get("palette_hex", []) if p not in ("#000000",)] or ["#402000", "#e0c0a0"]
        top, bottom = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (pal[0], pal[-1]))
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        for y in range(h):
            t = y / h
            d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
        # a simple horizon and sun so camera moves are visible in the animatic
        d.rectangle([0, int(h * 0.62), w, h], fill=(40, 26, 14))
        d.ellipse([int(w * 0.55), int(h * 0.48), int(w * 0.75), int(h * 0.48) + int(w * 0.2)], fill=(255, 214, 150))
        for k in range(12):
            x = int(w * (0.05 + 0.08 * k))
            d.rectangle([x, int(h * 0.55), x + w // 60, int(h * 0.62)], fill=(20, 12, 6))
        out.parent.mkdir(parents=True, exist_ok=True)
        label = _label_png(w, h, prompt, out.with_suffix(".label.png"))
        base = img.convert("RGBA")
        base.alpha_composite(Image.open(label))
        base.convert("RGB").save(out)
        label.unlink()
        return out, "", 0.0


# camera move -> eased crop path for the animatic (start scale, end scale, start x, end x as fractions)
_ANIMATIC_MOVES = {
    "push_in": (1.0, 1.25, 0.5, 0.5), "pull_out": (1.25, 1.0, 0.5, 0.5), "track_lateral": (1.15, 1.15, 0.3, 0.7),
    "low_heroic_reveal": (1.2, 1.05, 0.5, 0.5), "crane_up": (1.15, 1.15, 0.5, 0.5),
    "orbit_subtle": (1.12, 1.18, 0.4, 0.6), "ground_track": (1.15, 1.15, 0.7, 0.3),
    "handheld_stabilized": (1.1, 1.12, 0.48, 0.52), "rack_focus": (1.05, 1.05, 0.5, 0.5),
    "locked_off": (1.05, 1.05, 0.5, 0.5),
}


class Animatic:
    """Moves a crop window over the keyframe along the planned camera path, with smoothstep easing."""
    name = "animatic"
    style = "generic"

    def __init__(self, s: Settings, move: str = "push_in"):
        self.s, self.move = s, move
        self.caps = Capabilities(image_to_video=True, durations=[2, 3, 4, 5, 6], aspect_ratios=["9:16", "16:9"],
                                 price_per_second=0.0, placeholder=True)

    def validate_config(self) -> list[str]:
        return []

    def validate_credentials(self) -> list[str]:
        return []

    def estimate(self, seconds: float) -> float:
        return 0.0

    def generate(self, req: VideoRequest) -> VideoResult:
        gen = next((d for d in self.caps.durations if d >= req.seconds), self.caps.durations[-1])
        fps = self.s.reel.fps
        w, h = (1280, 720) if req.aspect == "16:9" else (720, 1280)
        s0, s1, x0, x1 = _ANIMATIC_MOVES.get(self.move, _ANIMATIC_MOVES["push_in"])
        n = gen * fps
        e = f"(3*pow(on/{n},2)-2*pow(on/{n},3))"                      # smoothstep ease-in/out
        z = f"({s0}+({s1}-{s0})*{e})"
        x = f"(iw-iw/zoom)*({x0}+({x1}-{x0})*{e})"
        y = "(ih-ih/zoom)*(0.5" + ("-0.5*" + e if self.move in ("crane_up", "low_heroic_reveal") else "") + "+0.0)"
        req.out.parent.mkdir(parents=True, exist_ok=True)
        src = req.image_path
        # the label goes on after the move so no crop can push it out of frame
        label = _label_png(w, h, "", req.out.with_name("animatic_label.png"))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(src), "-i", str(label),
                        "-filter_complex",
                        f"[0:v]scale={w * 2}:{h * 2},zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps}[m];"
                        "[m][1:v]overlay=0:0,format=yuv420p[v]", "-map", "[v]", "-frames:v", str(n),
                        "-c:v", "libx264", "-preset", "ultrafast", str(req.out)], check=True)
        label.unlink()
        return VideoResult(req.out, "local", "animatic", gen, 0.0, {"move": self.move}, placeholder=True)


class LocalCommand:
    """Runs a user-configured command per shot. Placeholders: {image} {prompt_file} {negative_file} {seconds}
    {aspect} {width} {height} {fps} {out}. The command must write an mp4 to {out}. Nothing is assumed about the
    model behind it; the clip is checked like any other take."""
    style = "generic"

    def __init__(self, s: Settings):
        self.s, self.cfg = s, s.local
        self.name = "local"
        self.caps = Capabilities(image_to_video=True, negative_prompt=True, durations=self.cfg.durations,
                                 aspect_ratios=["9:16", "16:9"], price_per_second=0.0,
                                 max_prompt_chars=self.cfg.max_prompt_chars)

    def validate_config(self) -> list[str]:
        if not self.cfg.video_command:
            return ["local.video_command is not set in reels.yaml"]
        return [] if "{out}" in self.cfg.video_command else ["local.video_command must contain {out}"]

    def validate_credentials(self) -> list[str]:
        return []

    def estimate(self, seconds: float) -> float:
        return 0.0

    def generate(self, req: VideoRequest) -> VideoResult:
        issues = self.validate_config()
        if issues:
            raise RuntimeError(issues[0])
        gen = next((d for d in self.cfg.durations if d >= req.seconds), self.cfg.durations[-1])
        req.out.parent.mkdir(parents=True, exist_ok=True)
        pf, nf = req.out.with_name("prompt.txt"), req.out.with_name("negative.txt")
        pf.write_text(req.prompt, encoding="utf-8")
        nf.write_text(req.negative, encoding="utf-8")
        w, h = (1280, 720) if req.aspect == "16:9" else (720, 1280)
        cmd = self.cfg.video_command.format(image=req.image_path, prompt_file=pf, negative_file=nf, seconds=gen,
                                            aspect=req.aspect, width=w, height=h, fps=self.s.reel.fps, out=req.out)
        subprocess.run(cmd, shell=True, check=True, timeout=self.cfg.timeout_seconds)
        if not req.out.exists():
            raise RuntimeError(f"local command finished but wrote no file at {req.out}")
        return VideoResult(req.out, "local", "local-command", gen, 0.0, {"command": cmd})


class UserKeyframe:
    """A keyframe you uploaded for a shot; used as-is for every new take of that shot."""
    name = "user-keyframe"
    placeholder = False

    def __init__(self, path: Path):
        self.path = path

    def generate(self, prompt: str, aspect: str, refs: list[Path], out: Path) -> tuple[Path, str, float]:
        import shutil
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path, out)
        return out, "", 0.0


class LocalImageCommand:
    name = "local-image"
    placeholder = False

    def __init__(self, s: Settings):
        self.s = s

    def generate(self, prompt: str, aspect: str, refs: list[Path], out: Path) -> tuple[Path, str, float]:
        cmd_t = self.s.local.image_command
        if not cmd_t:
            raise RuntimeError("local.image_command is not set in reels.yaml")
        out.parent.mkdir(parents=True, exist_ok=True)
        pf = out.with_name("keyframe_prompt.txt")
        pf.write_text(prompt, encoding="utf-8")
        w, h = (1280, 720) if aspect == "16:9" else (720, 1280)
        cmd = cmd_t.format(prompt_file=pf, refs=" ".join(str(r) for r in refs), aspect=aspect, width=w, height=h,
                           out=out)
        subprocess.run(cmd, shell=True, check=True, timeout=self.s.local.timeout_seconds)
        if not out.exists():
            raise RuntimeError(f"local image command wrote no file at {out}")
        return out, "", 0.0


def video_providers(s: Settings, fal: Callable[[], Fal]) -> dict[str, object]:
    out: dict[str, object] = {vm.id: FalVideo(vm, fal) for vm in s.fal.video_models}
    if s.local.video_command:
        out["local"] = LocalCommand(s)
    out["animatic"] = Animatic(s)
    return out


def check(s: Settings) -> list[str]:
    """Config and credential checks for every provider, without spending anything."""
    lines = []
    for name, p in video_providers(s, lambda: None).items():   # type: ignore[arg-type]
        issues = p.validate_config() + p.validate_credentials()
        caps = p.caps
        lines.append(f"{name}: {'OK' if not issues else '; '.join(issues)} | durations {caps.durations}, "
                     f"aspects {caps.aspect_ratios}, negative_prompt={caps.negative_prompt}, "
                     f"native_audio={caps.native_audio}" + (" | PLACEHOLDER ONLY" if caps.placeholder else ""))
    if not s.local.video_command:
        lines.append("local: not configured (optional free path: set local.video_command in reels.yaml)")
    lines.append("claude: " + ("OK" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY is not set "
                                                                                "(fine: the scheduled Claude Code task plans and reviews instead, see docs/ROUTINE.md)"))
    return lines
