"""fal.ai queue client with an on-disk result cache and two spend caps (per day and per reel)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx2 as httpx

from gita_studio.integrations.media import SpendLedger

from .settings import Settings


class BudgetExceeded(RuntimeError):
    pass


class Fal:
    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 downloader: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 today: Callable[[], str] | None = None):
        self.s = settings.fal
        key = os.environ.get("FAL_KEY")
        if client is None and not key:
            raise RuntimeError("FAL_KEY is not set")
        self.client = client or httpx.Client(headers={"Authorization": f"Key {key}"}, timeout=120)
        self.downloader = downloader or client or httpx.Client(timeout=300, follow_redirects=True)
        self.sleep = sleep
        self.cache = Path(settings.paths.cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ledger = SpendLedger(Path(settings.paths.out) / "spend.json", self.s.daily_budget_usd,
                                  today or (lambda: datetime.now(timezone.utc).date().isoformat()))
        self.reel_spent = 0.0

    def start_reel(self) -> None:
        self.reel_spent = 0.0

    def can_spend(self, cost: float) -> bool:
        return self.ledger.allows(cost) and self.reel_spent + cost <= self.s.reel_budget_usd + 1e-9

    def run(self, model: str, body: dict[str, Any], cost: float) -> dict[str, Any]:
        """Run a model once per unique input. Cached results cost nothing on re-runs."""
        key = hashlib.sha256(json.dumps([model, body], sort_keys=True).encode()).hexdigest()
        hit = self.cache / f"{key}.json"
        if hit.exists():
            return json.loads(hit.read_text())
        if not self.can_spend(cost):
            raise BudgetExceeded(f"{model}: ${cost:.2f} would exceed the daily (${self.s.daily_budget_usd}) "
                                 f"or per-reel (${self.s.reel_budget_usd}) budget")
        out = self._queue(model, body)
        self.ledger.charge(cost)
        self.reel_spent += cost
        hit.write_text(json.dumps(out))
        return out

    def _queue(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
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
            if waited >= self.s.timeout_seconds:
                raise RuntimeError(f"{model}: timed out after {waited:.0f}s")
            self.sleep(self.s.poll_seconds)
            waited += self.s.poll_seconds
        out = self.client.get(job["response_url"])
        out.raise_for_status()
        return out.json()

    def download(self, url: str, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if url.startswith("data:"):
            path.write_bytes(base64.b64decode(url.split(",", 1)[1]))
            return path
        resp = self.downloader.get(url)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return path


def data_uri(path: Path, max_side: int = 1024) -> str:
    """Inline a local image for fal (resized JPEG keeps requests small)."""
    from io import BytesIO

    from PIL import Image
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def check_models(settings: Settings) -> list[str]:
    """Compare configured field names with fal's published OpenAPI schema for each model (best effort)."""
    wanted = {settings.fal.keyframe_model: ["prompt", settings.fal.keyframe_refs_field, "aspect_ratio"],
              settings.fal.refs_model: ["prompt", "aspect_ratio"]}
    for vm in settings.fal.video_models:
        wanted[vm.id] = [f for f in ["prompt", "image_url", "duration", "resolution", "aspect_ratio"]
                         if f not in vm.omit] + (["generate_audio"] if vm.native_audio else []) + (
            ["negative_prompt"] if vm.negative_prompt else []) + ([vm.seed_field] if vm.seed_field else [])
    if settings.fal.music_model:
        wanted[settings.fal.music_model] = ["composition_plan", "respect_sections_durations"]
    report = []
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        for model, fields in wanted.items():
            try:
                r = c.get("https://fal.ai/api/openapi/queue/openapi.json", params={"endpoint_id": model})
                r.raise_for_status()
                schemas = r.json().get("components", {}).get("schemas", {})
                props = set().union(*(set(s.get("properties", {})) for n, s in schemas.items() if "Input" in n))
                missing = [f for f in fields if f not in props]
                report.append(f"{model}: " + ("OK" if not missing else f"MISSING {missing}; available {sorted(props)}"))
            except Exception as e:
                report.append(f"{model}: could not check ({e})")
    return report
