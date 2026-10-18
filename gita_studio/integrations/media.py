"""Media generation backends.

`prompt_only` writes the art director's prompts to disk so you can run them through any image,
video, voice or music generator. `webhook` hands them to an automation (n8n, Make, Zapier or
your own service) that calls your generator of choice and returns asset URLs. `fal` renders
stills and clips on fal.ai, downloads them next to the prompts, and stops at a daily budget.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx2 as httpx

from ..config import Config
from .webhook import post_json

log = logging.getLogger(__name__)


class MediaProvider(Protocol):
    def render(self, post_id: int, package: dict[str, Any]) -> dict[str, Any]: ...


class PromptOnlyMedia:
    def __init__(self, config: Config):
        self.root = Path(config.paths.media)

    def render(self, post_id: int, package: dict[str, Any]) -> dict[str, Any]:
        folder = self.root / f"post-{post_id:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        visuals = package["visuals"]
        (folder / "visual_plan.json").write_text(json.dumps(visuals, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"# Post {post_id} — style guide\n\n{visuals['style_guide']}\n"]
        for a in visuals["assets"]:
            lines.append(f"## Scene {a['scene_index']} · {a['kind']} · {a['aspect']}\n\n{a['prompt']}\n")
            if a.get("negative_prompt"):
                lines.append(f"Negative: {a['negative_prompt']}\n")
        (folder / "prompts.md").write_text("\n".join(lines), encoding="utf-8")
        return {"provider": "prompt_only", "folder": str(folder), "assets": []}


class WebhookMedia:
    def __init__(self, config: Config):
        self.config = config

    def render(self, post_id: int, package: dict[str, Any]) -> dict[str, Any]:
        result = post_json("MEDIA_WEBHOOK_URL", {"post_id": post_id, "visuals": package["visuals"],
                                                 "script": package["script"]}, timeout=600)
        # Expected response: {"assets": [{"scene_index": 0, "kind": "video", "url": "https://..."}]}
        return {"provider": "webhook", **result}


IMAGE_ASPECTS = {"21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16"}
VIDEO_ASPECTS = {"16:9", "9:16"}
VIDEO_DURATIONS = (4, 6, 8)


class SpendLedger:
    """Running fal spend per UTC day, kept in media/spend.json so it survives between runs."""

    def __init__(self, path: Path, budget: float, today: Callable[[], str]):
        self.path, self.budget, self.today = path, budget, today
        self.days: dict[str, float] = json.loads(path.read_text()) if path.exists() else {}

    @property
    def spent(self) -> float:
        return self.days.get(self.today(), 0.0)

    def allows(self, cost: float) -> bool:
        return self.spent + cost <= self.budget + 1e-9

    def charge(self, cost: float) -> None:
        self.days[self.today()] = round(self.spent + cost, 4)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.days, indent=2))


class FalMedia:
    """Renders image assets with an image model and video assets as still -> image-to-video clip."""

    def __init__(self, config: Config, client: httpx.Client | None = None, downloader: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, today: Callable[[], str] | None = None):
        self.settings = config.media.fal
        self.root = Path(config.paths.media)
        self.prompts = PromptOnlyMedia(config)
        key = os.environ.get("FAL_KEY")
        if client is None and not key:
            raise RuntimeError("FAL_KEY is not set")
        self.client = client or httpx.Client(headers={"Authorization": f"Key {key}"}, timeout=120)
        # Separate client without the key: finished files are served from fal's public CDN.
        self.downloader = downloader or client or httpx.Client(timeout=300, follow_redirects=True)
        self.sleep = sleep
        self.ledger = SpendLedger(self.root / "spend.json", self.settings.daily_budget_usd,
                                  today or (lambda: datetime.now(timezone.utc).date().isoformat()))

    def render(self, post_id: int, package: dict[str, Any]) -> dict[str, Any]:
        result = self.prompts.render(post_id, package)
        folder = Path(result["folder"])
        style = package["visuals"]["style_guide"]
        assets = []
        for a in package["visuals"]["assets"]:
            name = f"scene-{a['scene_index']:02d}-{a['kind']}"
            entry = {"scene_index": a["scene_index"], "kind": a["kind"]}
            if a["kind"] not in ("image", "video"):
                assets.append({**entry, "status": "skipped", "reason": "not rendered by fal (audio comes later)"})
                continue
            seconds = self._duration(a) if a["kind"] == "video" else 0
            cost = self.settings.image_price_usd + seconds * self.settings.video_price_per_second_usd
            if not self.ledger.allows(cost):
                log.warning("fal budget reached (%.2f of %.2f USD); skipping %s of post %s",
                            self.ledger.spent, self.settings.daily_budget_usd, name, post_id)
                assets.append({**entry, "status": "skipped", "reason": "daily budget reached"})
                continue
            try:
                still = self._image(a, style)
                self.ledger.charge(self.settings.image_price_usd)
                entry["image_url"] = still
                entry["file"] = self._download(still, folder / f"{name}.png")
                if a["kind"] == "video":
                    clip = self._video(a, style, still, seconds)
                    self.ledger.charge(seconds * self.settings.video_price_per_second_usd)
                    entry.update(url=clip, seconds=seconds, file=self._download(clip, folder / f"{name}.mp4"))
                else:
                    entry["url"] = still
                assets.append({**entry, "status": "rendered"})
            except (httpx.HTTPError, RuntimeError, KeyError) as e:
                log.error("fal render failed for %s of post %s: %s", name, post_id, e)
                assets.append({**entry, "status": "failed", "error": str(e)[:300]})
        return {"provider": "fal", "folder": str(folder), "assets": assets,
                "spent_today_usd": self.ledger.spent}

    # --- fal calls ----------------------------------------------------------------------
    def _image(self, a: dict[str, Any], style: str) -> str:
        prompt = f"{style}\n\n{a['prompt']}"
        if a.get("negative_prompt"):
            prompt += f"\n\nAvoid: {a['negative_prompt']}"
        out = self._run(self.settings.image_model, {
            "prompt": prompt, "num_images": 1, "resolution": self.settings.image_resolution,
            "aspect_ratio": a["aspect"] if a["aspect"] in IMAGE_ASPECTS else "auto"})
        return out["images"][0]["url"]

    def _video(self, a: dict[str, Any], style: str, image_url: str, seconds: int) -> str:
        body = {"prompt": f"{style}\n\n{a['prompt']}", "image_url": image_url, "duration": f"{seconds}s",
                "resolution": self.settings.video_resolution, "generate_audio": self.settings.video_audio,
                "aspect_ratio": a["aspect"] if a["aspect"] in VIDEO_ASPECTS else "auto"}
        if a.get("negative_prompt"):
            body["negative_prompt"] = a["negative_prompt"]
        return self._run(self.settings.video_model, body)["video"]["url"]

    def _run(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        """Submit to fal's queue, poll until done, return the model output."""
        resp = self.client.post(f"https://queue.fal.run/{model}", json=body)
        resp.raise_for_status()
        job = resp.json()
        waited = 0.0
        while True:
            status = self.client.get(job["status_url"])
            status.raise_for_status()
            state = status.json()
            if state.get("status") == "COMPLETED":
                if state.get("error"):
                    raise RuntimeError(f"{model}: {state.get('error_type')}: {state['error']}")
                break
            if waited >= self.settings.timeout_seconds:
                raise RuntimeError(f"{model}: timed out after {waited:.0f}s")
            self.sleep(self.settings.poll_seconds)
            waited += self.settings.poll_seconds
        out = self.client.get(job["response_url"])
        out.raise_for_status()
        return out.json()

    def _download(self, url: str, path: Path) -> str:
        resp = self.downloader.get(url)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return str(path)

    @staticmethod
    def _duration(a: dict[str, Any]) -> int:
        want = a.get("duration_seconds") or 0
        return next((d for d in VIDEO_DURATIONS if want <= d), VIDEO_DURATIONS[-1])


def get_media_provider(config: Config) -> MediaProvider:
    return {"prompt_only": PromptOnlyMedia, "webhook": WebhookMedia,
            "fal": FalMedia}[config.media.provider](config)
