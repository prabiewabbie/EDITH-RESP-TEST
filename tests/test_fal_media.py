import json
from pathlib import Path

import httpx2 as httpx
import pytest

from gita_studio.config import load_config
from gita_studio.integrations.media import FalMedia, get_media_provider

ROOT = Path(__file__).resolve().parents[1]

PACKAGE = {"visuals": {"style_guide": "painterly realism, dusk indigo and temple gold", "assets": [
    {"kind": "image", "scene_index": 0, "prompt": "chariot frozen mid-charge", "negative_prompt": "gore",
     "aspect": "9:16", "duration_seconds": 0},
    {"kind": "video", "scene_index": 1, "prompt": "light blooms across the field", "negative_prompt": "",
     "aspect": "9:16", "duration_seconds": 5},
    {"kind": "voiceover", "scene_index": 1, "prompt": "narration", "negative_prompt": "",
     "aspect": "9:16", "duration_seconds": 5},
]}}


class FakeFal:
    """Mimics fal's queue API and CDN; records every request body."""

    def __init__(self, fail_model=None):
        self.submitted = []
        self.fail_model = fail_model
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host == "queue.fal.run" and request.method == "POST":
            model = request.url.path.lstrip("/")
            self.submitted.append((model, json.loads(request.content)))
            base = f"https://queue.fal.run/{model}/requests/r{len(self.submitted)}"
            return httpx.Response(200, json={"request_id": "r", "status_url": base + "/status",
                                             "response_url": base})
        if url.endswith("/status"):
            self.polls += 1
            model = self.submitted[-1][0]
            if self.polls % 2:
                return httpx.Response(200, json={"status": "IN_PROGRESS"})
            if model == self.fail_model:
                return httpx.Response(200, json={"status": "COMPLETED", "error": "boom", "error_type": "x"})
            return httpx.Response(200, json={"status": "COMPLETED"})
        if request.url.host == "queue.fal.run":
            if "image-to-video" in url:
                return httpx.Response(200, json={"video": {"url": "https://v3.fal.media/clip.mp4"}})
            return httpx.Response(200, json={"images": [{"url": "https://v3.fal.media/still.png"}]})
        if request.url.host == "v3.fal.media":
            return httpx.Response(200, content=b"bytes-of-" + request.url.path.encode())
        raise AssertionError(url)


def make(tmp_path, fake, budget=10.0, day="2026-10-08"):
    config = load_config(ROOT / "config.yaml")
    config.paths.media = str(tmp_path / "media")
    config.media.fal.daily_budget_usd = budget
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return FalMedia(config, client=client, sleep=lambda s: None, today=lambda: day)


def test_renders_stills_and_clips_and_tracks_spend(tmp_path):
    fake = FakeFal()
    result = make(tmp_path, fake).render(7, PACKAGE)

    image, video, voice = result["assets"]
    assert image["status"] == "rendered" and Path(image["file"]).read_bytes() == b"bytes-of-/still.png"
    assert video["status"] == "rendered" and video["seconds"] == 6, "5s rounds up to fal's 6s option"
    assert Path(video["file"]).name == "scene-01-video.mp4"
    assert voice["status"] == "skipped"

    models = [m for m, _ in fake.submitted]
    assert models == ["fal-ai/nano-banana-2", "fal-ai/nano-banana-2", "fal-ai/veo3.1/fast/image-to-video"]
    first_image = fake.submitted[0][1]
    assert "dusk indigo" in first_image["prompt"] and "Avoid: gore" in first_image["prompt"]
    assert first_image["aspect_ratio"] == "9:16"
    clip = fake.submitted[2][1]
    assert clip["image_url"] == "https://v3.fal.media/still.png"
    assert clip["generate_audio"] is False and clip["duration"] == "6s"

    # 2 stills at $0.08 + 6s of video at $0.10
    assert result["spent_today_usd"] == pytest.approx(0.76)
    assert json.loads((tmp_path / "media" / "spend.json").read_text()) == {"2026-10-08": 0.76}
    assert (tmp_path / "media" / "post-00007" / "prompts.md").exists()


def test_daily_budget_is_a_hard_stop(tmp_path):
    fake = FakeFal()
    # Enough for the still, not for the still + 6s clip.
    result = make(tmp_path, fake, budget=0.5).render(1, PACKAGE)
    assert [a["status"] for a in result["assets"]] == ["rendered", "skipped", "skipped"]
    assert result["assets"][1]["reason"] == "daily budget reached"
    assert all("image-to-video" not in m for m, _ in fake.submitted)

    # Spend carries over within the same day, and resets the next day.
    again = make(tmp_path, FakeFal(), budget=0.5).render(2, PACKAGE)
    assert again["assets"][0]["status"] == "rendered" and again["spent_today_usd"] == pytest.approx(0.16)
    tomorrow = make(tmp_path, FakeFal(), budget=0.5, day="2026-10-09").render(3, PACKAGE)
    assert tomorrow["spent_today_usd"] == pytest.approx(0.08)


def test_failed_job_is_recorded_not_raised(tmp_path):
    fake = FakeFal(fail_model="fal-ai/veo3.1/fast/image-to-video")
    result = make(tmp_path, fake).render(4, PACKAGE)
    assert result["assets"][1]["status"] == "failed" and "boom" in result["assets"][1]["error"]
    # Only the stills were charged.
    assert result["spent_today_usd"] == pytest.approx(0.16)


def test_fal_provider_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    config = load_config(ROOT / "config.yaml")
    config.media.provider = "fal"
    with pytest.raises(RuntimeError, match="FAL_KEY"):
        get_media_provider(config)
