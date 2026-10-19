"""End-to-end tests for gita_reels with fake Claude and fake fal, but real ffmpeg and real media files."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx2 as httpx
import pytest

from gita_reels import settings as settings_mod
from gita_reels.fal import Fal
from gita_reels.pipeline import Studio
from gita_reels.writer import Issue, PlanReview, ReelPlan, Shot, VisualQC

ROOT = Path(__file__).resolve().parents[1]
QUOTE = "Your right is to the action alone, never to its fruits."
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    clip = d / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=720x1280:r=24:d=4",
                    "-f", "lavfi", "-i", "sine=f=220:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    wide = d / "wide.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(wide)], check=True)
    png = d / "still.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=orange:s=720x1280",
                    "-frames:v", "1", str(png)], check=True)
    return {"clip": clip.read_bytes(), "wide": wide.read_bytes(), "png": png.read_bytes()}


def plan(**over) -> ReelPlan:
    shots = [Shot(purpose=p, characters=c, keyframe_prompt=f"frame {i}", motion_prompt="Wind moves the banners",
                  camera="slow push-in", on_screen_seconds=2.5, framing=f, transition_in=t, narration="")
             for i, (p, c, f, t) in enumerate([
                 ("hero_closeup", ["arjuna"], "vertical", "cut"), ("wide", [], "rotated_wide", "cut"),
                 ("macro_insert", [], "vertical", "cut"), ("low_angle_detail", ["krishna"], "vertical", "flash"),
                 ("prop_detail", ["arjuna"], "vertical", "cut"), ("establishing", [], "vertical", "cut"),
                 ("reveal", ["krishna", "arjuna"], "vertical", "cut")])]
    data = dict(title="Act without clinging", description="Krishna's counsel in chapter 2.", tags=["gita"],
                music_mood="epic", shots=shots, verse_shot_index=5, verse_line=QUOTE)
    data.update(over)
    return ReelPlan(**data)


class FakeClaude:
    def __init__(self, plan_obj=None, qc_scores=None):
        self.plan_obj = plan_obj or plan()
        self.qc_scores = list(qc_scores or [])
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.qc_calls = 0

    def plan(self, episode, verse, feedback=None):
        return self.plan_obj.model_copy(deep=True)

    def review(self, plan, episode, verse):
        return PlanReview(fidelity_score=9, respect_score=10, issues=[], verdict="approve")

    def qc(self, frames, refs, shot):
        self.qc_calls += 1
        score = self.qc_scores.pop(0) if self.qc_scores else 9
        return VisualQC(identity=score, anatomy=score, artifacts=score, respect=10, prompt_match=score,
                        problems=[] if score >= 7 else ["face drift"], retry_hint="" if score >= 7 else "keep face")

    def cost_usd(self):
        return 0.0


class FakeFal:
    def __init__(self, media):
        self.media, self.submitted, self.n = media, [], 0

    def __call__(self, request):
        url = str(request.url)
        if request.url.host == "queue.fal.run" and request.method == "POST":
            body = json.loads(request.content)
            self.submitted.append((request.url.path.lstrip("/"), body))
            self.n += 1
            base = f"https://queue.fal.run/x/requests/r{self.n}"
            kind = "wide" if body.get("aspect_ratio") == "16:9" else "clip"
            is_video = "image-to-video" in request.url.path
            return httpx.Response(200, json={"status_url": f"{base}/status", "response_url": f"{base}?k={kind}&v={int(is_video)}"})
        if url.endswith("/status"):
            return httpx.Response(200, json={"status": "COMPLETED"})
        if request.url.host == "queue.fal.run":
            q = dict(request.url.params)
            if q["v"] == "1":
                return httpx.Response(200, json={"video": {"url": f"https://v3.fal.media/{q['k']}.mp4"}})
            return httpx.Response(200, json={"images": [{"url": "https://v3.fal.media/still.png"}]})
        if request.url.host == "v3.fal.media":
            name = request.url.path.strip("/").split(".")[0]
            return httpx.Response(200, content=self.media["png" if name == "still" else name])
        raise AssertionError(url)


@pytest.fixture
def cfg(tmp_path):
    c = settings_mod.load(ROOT / "reels.yaml")
    c.paths.out, c.paths.cache = str(tmp_path / "reels"), str(tmp_path / "cache")
    c.paths.state, c.paths.music = str(tmp_path / "state.json"), str(tmp_path / "music")
    style = tmp_path / "style"
    shutil.copytree(ROOT / "style", style, ignore=shutil.ignore_patterns("refs"))
    c.paths.style = str(style)
    c.paths.verses = str(ROOT / "data" / "verses.sample.json")
    c.reel.preset = "ultrafast"
    return c


def studio(cfg, media, claude=None, budget=None):
    fake = FakeFal(media)
    if budget is not None:
        cfg.fal.reel_budget_usd = budget
    fal = Fal(cfg, client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None,
              today=lambda: "2026-10-19")
    return Studio(cfg, claude=claude or FakeClaude(), fal=fal), fake


def probe(path, entry):
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", entry, "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, check=True).stdout.split()


def test_full_reel_is_animated_editable_and_ready(cfg, media):
    st, fake = studio(cfg, media)
    folder = st.make("e07")
    meta = json.loads((folder / "project.json").read_text())
    assert meta["status"] == "ready", meta.get("error")

    final = folder / "final.mp4"
    w, h = probe(final, "stream=width,height")[0].split(",")[:2]
    assert (w, h) == ("1080", "1920")
    assert abs(float(probe(final, "format=duration")[0]) - 17.5) < 0.3     # 7 shots x 2.5 s
    assert len(list((folder / "clips").glob("shot_*.mp4"))) == 7            # conformed, editable shots
    for stem in ("music.wav", "ambience.wav", "mix.wav"):
        assert (folder / "audio" / stem).exists()
    assert QUOTE in (folder / "captions.srt").read_text()

    import opentimelineio as otio
    tl = otio.adapters.read_from_file(str(folder / "timeline.otio"))
    assert [t.name for t in tl.tracks][:3] == ["V1 shots", "V2 titles", "V3 brand"]
    assert len(tl.tracks[0]) == 7
    xml = (folder / "timeline.xml").read_text()
    assert "<xmeml" in xml and "<width>1080</width>" in xml and "<height>1920</height>" in xml

    # reference sheets made once for the two characters, keyframes use them as inputs
    assert {p.stem for p in (Path(cfg.paths.style) / "refs").glob("*.png")} == {"arjuna", "krishna"}
    edit_calls = [b for m, b in fake.submitted if m == cfg.fal.keyframe_model]
    assert edit_calls and all(b[cfg.fal.keyframe_refs_field][0].startswith("data:image/jpeg") for b in edit_calls)
    assert meta["youtube"]["description"].count("AI-generated") == 1
    assert meta["fal_spent_usd"] > 0


def test_rerun_hits_cache_and_costs_nothing(cfg, media):
    st, fake = studio(cfg, media)
    st.make("e07")
    first = len(fake.submitted)
    st2, fake2 = studio(cfg, media)
    meta = json.loads((st2.make("e07") / "project.json").read_text())
    assert meta["status"] == "ready" and fake2.submitted == [] and first > 0
    assert meta["fal_spent_usd"] == 0


def test_qc_failure_retries_then_gives_up(cfg, media):
    claude = FakeClaude(qc_scores=[3, 3, 3])       # shot 1 keyframe fails on every attempt
    st, _ = studio(cfg, media, claude=claude)
    meta = json.loads((st.make("e07") / "project.json").read_text())
    assert meta["status"] == "failed_production" and "shot 1" in meta["error"]
    assert claude.qc_calls == cfg.fal.max_retries_per_shot + 1


def test_reel_budget_cap_stops_spending(cfg, media):
    st, fake = studio(cfg, media, budget=1.0)
    meta = json.loads((st.make("e07") / "project.json").read_text())
    assert meta["status"] == "failed_production" and "budget" in meta["error"]
    assert meta["fal_spent_usd"] <= 1.0


def test_guards_on_verse_length_and_framing(cfg, media):
    bad = plan(verse_line="Krishna says hustle harder", shots=plan().shots * 3)
    st, _ = studio(cfg, media, claude=FakeClaude(plan_obj=bad))
    p, _, _ = st.plan(st.episode("e07"))
    assert p.verse_line == "" and p.verse_shot_index == -1                 # not a verbatim excerpt: dropped
    assert sum(s.on_screen_seconds for s in p.shots) <= cfg.reel.max_seconds + 0.01
    assert sum(s.framing == "rotated_wide" for s in p.shots) <= 1


def test_review_rejection_spends_nothing(cfg, media):
    class Rejecting(FakeClaude):
        def review(self, plan, episode, verse):
            return PlanReview(fidelity_score=4, respect_score=9, verdict="reject",
                              issues=[Issue(severity="blocker", problem="invented dialogue", fix="remove")])
    st, fake = studio(cfg, media, claude=Rejecting())
    meta = json.loads((st.make("e07") / "project.json").read_text())
    assert meta["status"] == "rejected_by_review" and fake.submitted == []


def test_autopilot_walks_the_storyline_and_publishes(cfg, media, monkeypatch):
    uploaded = []
    monkeypatch.setattr("gita_reels.publish.upload", lambda folder, privacy, publish_at=None:
                        uploaded.append((folder, privacy)) or "https://youtube.com/shorts/x")
    st, _ = studio(cfg, media)
    r = st.autopilot()
    assert r["episode"] == "e01" and r["status"] == "published" and uploaded[0][1] == "public"
    assert st.state()["next"] == 1
