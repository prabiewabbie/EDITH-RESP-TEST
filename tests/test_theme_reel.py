"""A theme reel in split mode: queued with its own anchors, a cue sheet and hand-written prompts; produced, reviewed,
scored to the cue sheet, held for approval and released private. Fake fal, real ffmpeg."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx2 as httpx
import pytest

from gita_reels.fal import Fal
from gita_reels.pipeline import Studio
from gita_reels.review import loudness
from gita_reels.score import bed_request
from gita_reels.writer import CueSheet

from test_reels import FakeClaude, FakeFal, cfg, media, plan  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
VERSE = ("Thy business is with the action only, never with its fruits; so let not the fruit of action be thy "
         "motive, nor be thou to inaction attached.")


class MusicFal(FakeFal):
    def __init__(self, media, bed: bytes):
        super().__init__(media)
        self.media = {**media, "bed": bed}

    def __call__(self, request):
        if request.url.host == "queue.fal.run" and request.method == "GET" and "m=1" in str(request.url):
            return httpx.Response(200, json={"audio": {"url": "https://v3.fal.media/bed.mp3"}})
        if request.url.host == "queue.fal.run" and request.method == "POST" and "music" in request.url.path:
            self.submitted.append((request.url.path.lstrip("/"), json.loads(request.content)))
            return httpx.Response(200, json={"status_url": "https://queue.fal.run/x/requests/m/status",
                                             "response_url": "https://queue.fal.run/x/requests/m?m=1"})
        return super().__call__(request)


def theme_plan():
    p = plan(verse_shot_index=5, verse_line=VERSE)
    secs = [1.6, 2.4, 1.2, 0.6, 0.6, 3.0, 3.6]
    for sh, s in zip(p.shots, secs):
        sh.on_screen_seconds = s
    p.shots[0].screen_direction = p.shots[1].screen_direction = "left_to_right"
    p.shots[2].transition_in, p.shots[3].transition_in = "cut_on_action", "whip_pan"
    p.shots[3].ramp_at, p.shots[3].source_in = 0.2, 1.0
    p.shots[4].source_of, p.shots[4].source_in = 2, 2.5          # the burst's closing piece of shot 3's clip
    p.shots[4].transition_in = "smash_cut"
    p.shots[5].transition_in = "l_cut"
    p.shots[6].black_after, p.shots[6].model = 0.4, "bytedance/seedance-2.0/mini/image-to-video"
    p.hook_text, p.question_text = "POV: the arrow is already drawn", "Would you still draw if you knew?"
    p.verse_start, p.verse_end = 9.4, 13.4
    p.cue_sheet = CueSheet.model_validate({
        "key": "D minor", "tempo": 96, "styles": ["low strings", "dhol"], "avoid": [],
        "beats": [{"name": "hook", "start": 0, "end": 1.6, "intensity": 6, "instruments": "dhol hit, low strings",
                   "hits": [0.0]},
                  {"name": "build", "start": 1.6, "end": 4.0, "intensity": 4, "instruments": "string ostinato"},
                  {"name": "burst", "start": 4.0, "end": 6.4, "intensity": 9, "instruments": "taiko, brass",
                   "hits": [5.4]},
                  {"name": "silence", "start": 6.4, "end": 9.4, "intensity": 1, "instruments": "breath only"},
                  {"name": "release", "start": 9.4, "end": 13.0, "intensity": 7, "instruments": "male choir, strings",
                   "hits": [9.4]}],
        "risers": [5.4], "silence": {"start": 6.6, "end": 9.3, "keep": "heartbeat"}})
    return p


def test_theme_reel_is_scored_held_and_released_private(cfg, media, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("REELS_PRIVACY", "private")
    uploaded = []
    monkeypatch.setattr("gita_reels.publish.upload", lambda folder, privacy, publish_at=None:
                        uploaded.append((folder, privacy)) or "https://youtube.com/shorts/x")
    cfg.paths.queue = str(tmp_path / "queue")
    cfg.reel.min_seconds = 12
    cfg.paths.verses = str(Path(__file__).resolve().parents[1] / "data" / "verses.json")
    bed = tmp_path / "bed.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=147:d=14", "-ac", "2",
                    str(bed)], check=True)
    fake = MusicFal(media, bed.read_bytes())
    fal = Fal(cfg, client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None,
              today=lambda: "2026-10-27")
    st = Studio(cfg, fal=fal)
    p = theme_plan()
    anchors = {"arjuna": "ARJUNA: South Asian man about 30; long rectangular face"}
    q = Path(cfg.paths.queue)
    q.mkdir()
    review = FakeClaude().review(None, None, None)
    (q / "theme-battle-pov-247.json").write_text(json.dumps({
        "theme": {"verse": "2.47", "title": "Battle POV: the arrow", "brief": "Arjuna, arrow drawn",
                  "series": "Battle POV", "format": "POV", "characters": anchors},
        "plan": p.model_dump(), "review": review.model_dump(),
        "prompt_overrides": {"0": {"keyframe": "ARJUNA: anchor. Close on the drawn string.",
                                   "video": "ARJUNA: anchor. The string trembles."}},
        "caption": "POV: the arrow is already drawn.\nWould you still draw if you knew how it ends?"}))
    before = st.state()

    r = st.produce()
    assert r["status"] == "awaiting_qc", r
    assert not (q / "theme-battle-pov-247.json").exists() and (q / "done" / "theme-battle-pov-247.json").exists()
    project = st.pending()
    meta = project.meta()
    assert meta["kind"] == "theme" and meta["hold"] and meta["episode"]["verses"] == ["2.47"]
    assert json.loads((project.folder / "characters.json").read_text())["arjuna"] == anchors["arjuna"]
    assert project.plan().shots[3].on_screen_seconds == 0.6                  # sprint shots are not padded out
    assert not project.takes(4)                                             # reuses shot 3's take
    t0 = project.take(0, "t01")
    assert t0["prompts"]["keyframe"].startswith("ARJUNA: anchor.") and t0["prompts"]["video"].startswith("ARJUNA:")
    t6 = project.take(6, "t01")
    assert t6["model"] == "bytedance/seedance-2.0/mini/image-to-video"
    sent = next(b for path, b in fake.submitted if path.startswith("bytedance/seedance-2.0/mini"))
    assert sent["generate_audio"] is True and sent["duration"] == "5" and "negative_prompt" not in sent
    assert "Avoid:" in sent["prompt"] and "no music" in sent["prompt"]
    spent_after_produce = meta["fal_spent_usd"]
    assert spent_after_produce > 0

    for i in range(len(p.shots)):
        if i != 4:
            project.record_qc(i, "t01", passed=i != 1, notes=[] if i != 1 else ["the bow warps at the grip"])
    r = st.finish_pending()
    assert r["retaken"] == [2] and project.meta()["fal_spent_usd"] > spent_after_produce   # cumulative
    assert "the bow warps at the grip" in project.take(1, "t02")["prompts"]["keyframe"]
    project.record_qc(1, "t02", passed=True, notes=[])

    r = st.finish_pending()
    assert r["status"] == "ready" and "held" in r and not uploaded, r      # held: nothing uploaded
    assert st.state() == before                                            # the storyline did not move
    meta = project.meta()
    music_call = next(b for path, b in fake.submitted if "music" in path)
    assert music_call == bed_request(project.plan().cue_sheet, 13.0)
    assert sum(s["duration_ms"] for s in music_call["composition_plan"]["sections"]) == 13000
    assert all(s["duration_ms"] >= 3000 for s in music_call["composition_plan"]["sections"])
    final = Path(meta["edit"]["final"])
    assert abs(float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                     "csv=p=0", str(final)], capture_output=True, text=True).stdout) - 13.0) < 0.15
    for name in ("hook.png", "verse.png", "question.png"):
        assert (project.folder / "titles" / name).exists()
    rv = meta["final_review"]
    assert (project.folder / "review" / "contact_sheet.jpg").exists()
    assert (project.folder / "review" / "loudness.png").exists()
    assert abs(rv["integrated_lufs"] + 14) <= 1.0 and rv["true_peak_dbtp"] <= -0.9, rv
    assert rv["silence"]["drop_db"] >= 10, rv["silence"]
    assert "Translation: Annie Besant" in meta["youtube"]["description"]
    assert meta["youtube"]["description"].startswith("POV: the arrow is already drawn.")

    r = st.release()
    assert r["status"] == "published" and uploaded == [(project.folder, "private")]
    assert st.release()["status"] == "skipped"                             # once only


def test_cue_sections_merge_short_beats_and_fill_the_reel():
    p = theme_plan()
    secs = bed_request(p.cue_sheet, 13.0)["composition_plan"]["sections"]
    assert [s["duration_ms"] for s in secs] == [4000, 5400, 3600]
    assert "hook / build" == secs[0]["section_name"]


def test_black_frames_and_silence_are_measurable(tmp_path):
    """loudness() reads a curve the review can check."""
    a = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=2", "-f", "lavfi",
                    "-i", "anullsrc=r=48000:cl=mono", "-filter_complex", "[1:a]atrim=0:2[s];[0:a][s]concat=n=2:v=0:a=1",
                    str(a)], check=True)
    r = loudness(a)
    loud = [m for t, m in r["momentary"] if 0.5 < t < 1.9]
    quiet = [m for t, m in r["momentary"] if t > 2.6]
    assert min(loud) - max(quiet, default=-120) > 20
