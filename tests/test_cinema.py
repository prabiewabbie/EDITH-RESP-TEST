"""Cinematography layer: prompt composer, providers, takes, reference analysis, export validation and the UI.

Real ffmpeg and real files throughout; no network, no API keys (the animatic and local-command providers)."""
import json
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from gita_reels import settings as settings_mod
from gita_reels.cinema import keyframe_prompt, lint, negative, normalize, sections, video_prompt, SECTIONS
from gita_reels.pipeline import Studio
from gita_reels.providers import FalVideo, VideoRequest
from gita_reels.validate import validate_export
from gita_reels.writer import ReelPlan, Shot

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def cfg(tmp_path):
    c = settings_mod.load(ROOT / "reels.yaml")
    c.paths.out, c.paths.cache = str(tmp_path / "reels"), str(tmp_path / "cache")
    c.paths.state, c.paths.music, c.paths.sfx = str(tmp_path / "state.json"), str(tmp_path / "music"), str(tmp_path / "sfx")
    style = tmp_path / "style"
    shutil.copytree(ROOT / "style", style, ignore=shutil.ignore_patterns("refs"))
    c.paths.style = str(style)
    c.paths.verses = str(ROOT / "data" / "verses.sample.json")
    c.reel.preset = "ultrafast"
    c.reel.width, c.reel.height = 360, 640
    c.reel.min_seconds, c.reel.max_seconds = 6, 12
    return c


def shot(**over) -> Shot:
    d = dict(purpose="hero_closeup", characters=["arjuna"], subject="Arjuna lowers his bow, eyes wet",
             environment="Kurukshetra at dawn", composition="face on the right third, banner blur in foreground",
             camera_height="low", lens="portrait_85", focus="eyes", move="push_in",
             move_detail="from medium close-up to close-up over the shot", subject_motion="Breathes out slowly",
             lighting="golden_backlight", atmosphere="dust", materials="bronze armour, silk",
             end_frame="tight on his eyes", screen_direction="right_to_left", on_screen_seconds=2.5,
             framing="vertical", transition_in="cut", sfx="", narration="")
    d.update(over)
    return Shot(**d)


# --- prompt composer -----------------------------------------------------------------------
def test_sections_follow_the_required_order_and_are_explicit(cfg):
    sec = sections(shot(), cfg)
    assert list(sec) == SECTIONS
    assert "85 mm" in sec["lens"] and "f/2" in sec["focus"]
    assert "dollies" in sec["camera_move"] and "digital zoom" in sec["camera_move"]   # move vs zoom spelled out
    assert "behind" in sec["lighting"]                                                 # backlight, not front light
    assert "Arjuna, Pandava warrior" in sec["subject"]                                 # identity from the cast list


def test_lenses_and_moves_produce_different_language(cfg):
    a = video_prompt(shot(), cfg, image_to_video=False)
    b = video_prompt(shot(lens="normal_50", move="track_lateral"), cfg, image_to_video=False)
    assert "85 mm" in a and "50 mm" in b and "slides sideways" in b and "slides sideways" not in a


def test_image_to_video_prompt_leads_with_camera_and_keyframe_has_no_motion(cfg):
    v = video_prompt(shot(), cfg, style="veo")
    assert v.startswith("Slow push-in")
    k = keyframe_prompt(shot(), cfg)
    assert "first frame" in k and "Breathes out" not in k and "medium close-up" in k
    assert len(video_prompt(shot(), cfg, style="generic", max_chars=400)) <= 400


def test_negative_prompt_adapts_to_the_subject(cfg):
    bee = shot(purpose="macro_insert", characters=[], subject="a bee on a marigold", subject_motion="the bee lifts off")
    assert "malformed hands" not in negative(bee, cfg) and "extra legs" in negative(bee, cfg)
    assert "malformed hands" in negative(shot(), cfg)
    assert "random camera shake" not in negative(shot(move="handheld_stabilized"), cfg)


def test_unknown_presets_are_replaced_and_lint_reports(cfg):
    sh = shot(lens="anamorphic_40", move="drone")
    notes = normalize(sh, cfg)
    assert (sh.lens, sh.move) == ("portrait_85", "push_in") and len(notes) == 2
    plan = ReelPlan(title="t", description="d", tags=[], music_mood="epic", verse_shot_index=-1, verse_line="",
                    shots=[shot(lens="ultrawide_21"), shot(lens="ultrawide_21", screen_direction="left_to_right")])
    out = lint(plan)
    assert plan.shots[1].lens == "wide_32" and any("reverse screen direction" in n for n in out)


def test_fal_adapter_only_sends_supported_fields(cfg, tmp_path):
    vm = next(v for v in cfg.fal.video_models if "veo3.1" in v.id)
    req = VideoRequest(prompt="p", negative="n", seconds=3.1, aspect="1:1", out=tmp_path / "c.mp4",
                       image_url="https://x/k.png", seed=7)
    body = FalVideo(vm, lambda: None).body(req)
    assert "negative_prompt" not in body and "seed" not in body       # not confirmed in the model's schema
    assert body["aspect_ratio"] == "9:16" and body["duration"] == "4s"  # unsupported aspect falls back
    vm2 = vm.model_copy(update={"negative_prompt": True, "seed_field": "seed"})
    body2 = FalVideo(vm2, lambda: None).body(req)
    assert body2["negative_prompt"] == "n" and body2["seed"] == 7
    wan = next(v for v in cfg.fal.video_models if "wan-pro" in v.id)
    body3 = FalVideo(wan, lambda: None).body(req)
    assert "duration" not in body3 and "aspect_ratio" not in body3 and FalVideo(wan, lambda: None).estimate(3) == 0.16


# --- takes, approval, assembly -------------------------------------------------------------
def test_takes_never_overwrite_and_animatic_never_publishes(cfg):
    st = Studio(cfg)
    p = st.create("e07", planner="template")
    n = len(p.plan().shots)
    for i in range(n):
        t = st.take(p, i, "animatic")
        assert t["status"] == "ready" and t["placeholder"] and t["metrics"]["motion"] > 0
        p.select(i, t["id"])
    first = (p.shot_dir(0) / "t01" / "clip.mp4").read_bytes()
    t2 = st.take(p, 0, "animatic", hint="slower")
    assert t2["id"] == "t02" and (p.shot_dir(0) / "t01" / "clip.mp4").read_bytes() == first
    assert p.meta()["selected"]["0"] == "t01"                         # new take is not auto-approved
    p.reject(0, "t02")
    assert p.meta()["rejected"]["0"] == ["t02"]

    p.edit_shot(1, {"lens": "tele_120", "on_screen_seconds": 2.0}, {"video": "custom prompt"})
    assert p.plan().shots[1].lens == "tele_120" and p.take(1, "t01")["spec"]["lens"] != "tele_120"
    assert st.take(p, 1, "animatic")["prompts"]["video"] == "custom prompt"

    meta = st.finish(p)
    assert meta["status"] == "animatic" and meta["validation"]["ok"], meta["validation"]
    from gita_reels.publish import upload
    with pytest.raises(RuntimeError, match="not ready"):
        upload(p.folder, "private")


def test_import_and_local_command_are_free_paths(cfg, tmp_path):
    clip = tmp_path / "mine.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=360x640:r=24:d=3",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    cfg.local.video_command = (f"ffmpeg -y -loglevel error -loop 1 -i {{image}} -vf scale=360:640,format=yuv420p "
                               f"-t {{seconds}} -r {{fps}} {{out}}")
    cfg.local.image_command = (f"ffmpeg -y -loglevel error -f lavfi -i color=c=orange:s={{width}}x{{height}} "
                               f"-frames:v 1 {{out}}")
    st = Studio(cfg)
    p = st.create("e07", planner="template")
    t = st.import_take(p, 0, clip)
    assert t["status"] == "ready" and t["provider"] == "manual" and (p.shot_dir(0) / t["id"] / "keyframe.png").exists()
    t = st.take(p, 1, "local")
    assert t["status"] == "ready" and t["model"] == "local-command" and not t["placeholder"]
    assert (p.shot_dir(1) / t["id"] / "prompt.txt").read_text().startswith("Camera Move: Lateral")  # composed prompt handed over
    assert "almost no movement" in " ".join(t["metrics"]["notes"])     # a still loop is flagged


# --- reference analysis --------------------------------------------------------------------
def test_reference_analysis_finds_shots_motion_and_writes_contact_sheet(tmp_path):
    from gita_reels.analyze import analyze, apply_to_style
    parts = []
    specs = [("testsrc2=s=1440x1280:r=24:d=1.5", "crop=720:1280:x='t*300':y=0"),   # camera pans right
             ("mandelbrot=s=720x1280:r=24", "null"), ("smptebars=s=720x1280:r=24", "null")]
    for k, (src, vf) in enumerate(specs):
        p = tmp_path / f"p{k}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-t", "1.5", "-vf", vf,
                        "-pix_fmt", "yuv420p", "-r", "24", str(p)], check=True)
        parts.append(p)
    lst = tmp_path / "l.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    ref = tmp_path / "ref.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf",
                    "scale=720:1280,setsar=1", "-pix_fmt", "yuv420p", str(ref)], check=True)
    r = analyze(ref, tmp_path / "out")
    assert r["shot_count"] == 3 and abs(r["avg_shot_seconds"] - 1.5) < 0.2
    assert (tmp_path / "out" / "contact_sheet.jpg").exists() and len(list((tmp_path / "out" / "frames").glob("*.jpg"))) == 9
    assert "pan/track" in " ".join(r["shots"][0]["camera_motion"])
    assert "contact_sheet.jpg" in (tmp_path / "out" / "report.md").read_text()
    style = tmp_path / "style"
    style.mkdir()
    bible = json.loads(apply_to_style(r, style).read_text())
    assert bible["avg_shot_seconds"] == r["avg_shot_seconds"] and len(bible["reference_shots"]) == 3


# --- export validation ---------------------------------------------------------------------
def test_validation_catches_wrong_format_black_frames_and_clipping(tmp_path):
    bad = tmp_path / "bad.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=2",
                    "-f", "lavfi", "-i", "sine=f=440:d=2,volume=12", "-c:a", "aac", "-pix_fmt", "yuv420p",
                    "-shortest", str(bad)], check=True)
    r = validate_export(bad, 1080, 1920, 24, 2.0)
    failed = {c["name"] for c in r["checks"] if not c["ok"]}
    assert not r["ok"] and {"dimensions", "frame rate", "black frames", "audio clipping"} <= failed


# --- web studio ----------------------------------------------------------------------------
def test_studio_api_guards_and_edits(cfg):
    from http.server import ThreadingHTTPServer

    from gita_reels.ui import Jobs, make_handler
    st = Studio(cfg)
    p = st.create("e07", planner="template")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, str(ROOT / "reels.yaml"),
                                                               Jobs(Path(cfg.paths.out) / ".jobs", "x")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        ov = json.load(urllib.request.urlopen(base + "/api/overview"))
        assert "portrait_85" in ov["lenses"] and "animatic" in ov["providers"]
        assert "<title>Gita Studio</title>" in urllib.request.urlopen(base + "/").read().decode()
        url = f"{base}/api/projects/{p.folder.name}/shots/0/spec"
        body = json.dumps({"fields": {"move": "orbit_subtle"}}).encode()
        with pytest.raises(urllib.error.HTTPError) as e:          # no X-Studio header: refused
            urllib.request.urlopen(urllib.request.Request(url, body, method="POST"))
        assert e.value.code == 403
        req = urllib.request.Request(url, body, {"X-Studio": "1", "Content-Type": "application/json"}, method="POST")
        view = json.load(urllib.request.urlopen(req))
        assert view["shots"][0]["spec"]["move"] == "orbit_subtle" and "arcs around" in view["shots"][0]["composed"]["video"]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/files/projects/..%2F..%2Fetc%2Fpasswd")
        assert e.value.code == 404
    finally:
        httpd.shutdown()
