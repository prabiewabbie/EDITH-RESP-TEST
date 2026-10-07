"""Media generation backends.

`prompt_only` writes the art director's prompts to disk so you can run them through any image,
video, voice or music generator. `webhook` hands them to an automation (n8n, Make, Zapier or
your own service) that calls your generator of choice and returns asset URLs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ..config import Config
from .webhook import post_json


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


def get_media_provider(config: Config) -> MediaProvider:
    return {"prompt_only": PromptOnlyMedia, "webhook": WebhookMedia}[config.media.provider](config)
