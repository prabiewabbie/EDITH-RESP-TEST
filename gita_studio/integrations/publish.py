"""Publishing backends.

`outbox` (default) writes one ready-to-post folder per post and platform — a safe dry run.
`webhook` posts the package to a scheduler/automation (Buffer, Make, Zapier, n8n, or your own
service using the official YouTube Data, Instagram Graph, TikTok Content Posting and X APIs).
Routing publishing through those tools keeps platform OAuth tokens out of this codebase.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ..config import Config
from .webhook import post_json


class Publisher(Protocol):
    def publish(self, post_id: int, platform: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class OutboxPublisher:
    def __init__(self, config: Config):
        self.root = Path(config.paths.outbox)

    def publish(self, post_id: int, platform: str, payload: dict[str, Any]) -> dict[str, Any]:
        folder = self.root / f"post-{post_id:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{platform}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"provider": "outbox", "path": str(path)}


class WebhookPublisher:
    def __init__(self, config: Config):
        self.config = config

    def publish(self, post_id: int, platform: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"provider": "webhook", **post_json("PUBLISH_WEBHOOK_URL", {"post_id": post_id, "platform": platform,
                                                                            **payload})}


def get_publisher(config: Config) -> Publisher:
    return {"outbox": OutboxPublisher, "webhook": WebhookPublisher}[config.publishing.provider](config)


def build_payload(config: Config, package: dict[str, Any], platform: str) -> dict[str, Any]:
    copies = [c for c in package["distribution"]["copies"] if c["platform"] == platform]
    return {
        "platform": platform,
        "aspect": config.platforms[platform].aspect,
        "format": package["idea"]["format"],
        "verse_ref": f"BG {package['idea']['chapter']}.{package['idea']['verse']}",
        "copies": copies,  # one per language
        "script": package["script"],
        "media": package.get("media", {}),
        # Tells the downstream poster to set each platform's "AI-generated / altered content" flag.
        "ai_generated": True,
    }
