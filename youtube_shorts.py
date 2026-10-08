#!/usr/bin/env python3
"""
Gita Shorts: one file that writes, checks, renders and uploads Bhagavad Gita YouTube Shorts.

    python youtube_shorts.py make                 # write + review + render one Short into shorts/
    python youtube_shorts.py upload shorts/<dir>  # upload it (private by default)
    python youtube_shorts.py auto                 # make + upload as private, for a daily cron job

Setup (once):
    pip install anthropic pillow edge-tts google-api-python-client google-auth-oauthlib
    install ffmpeg (brew install ffmpeg / sudo apt install ffmpeg / winget install ffmpeg)
    export ANTHROPIC_API_KEY=sk-ant-...
    put verses in verses.json (format below) and client_secret.json from Google Cloud next to this file

verses.json: [{"chapter": 2, "verse": 47, "sanskrit": "...", "transliteration": "...",
               "translation": "...", "source": "Translator, edition (public domain / licensed)"}]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------------------------
# Settings: edit these
# ----------------------------------------------------------------------------------------
CHANNEL_NAME = "Gita Daily"
MODEL = "claude-opus-5-5"
VOICE = "en-IN-PrabhatNeural"        # edge-tts voice; try en-IN-NeerjaNeural, en-US-GuyNeural
VERSES_FILE = Path("verses.json")
OUT_DIR = Path("shorts")
MUSIC_FILE = Path("music.mp3")       # optional royalty-free background track, mixed quietly
CLIENT_SECRET = Path("client_secret.json")
TOKEN_FILE = Path("youtube_token.json")
MIN_SCORE = 8                         # reviewer score (0-10) required to render
W, H, FPS = 1080, 1920, 30
AI_DISCLOSURE = "Visuals and voice are AI-generated. Verse translation: {source}."

GUARDRAILS = """\
- The Bhagavad Gita is sacred to roughly a billion people. Be reverent, warm and clear.
- Quote only the verse translation given to you, word for word. Never quote from memory, never invent \
a "Krishna said" line, and always cite the reference (e.g. BG 2.47).
- Stay within mainstream traditional interpretation; where schools differ, don't pick one as "the" meaning.
- No medical, financial or political claims. Do not disparage any religion, tradition or community.
- Modern applications are welcome but must read as reflection, not scripture.
- Depict Krishna, Arjuna and all figures with dignity in traditional iconography; nothing sexualised, \
comedic or gory; no real people or celebrity likenesses."""


# ----------------------------------------------------------------------------------------
# Structured outputs
# ----------------------------------------------------------------------------------------
class Scene(BaseModel):
    narration: str = Field(description="Voice-over for this scene, 1-2 short sentences")
    on_screen_text: str = Field(description="Max 12 words shown on screen")
    image_prompt: str = Field(description="Detailed prompt for an AI image generator, 9:16, no text in image")


class Short(BaseModel):
    title: str = Field(description="YouTube title, under 70 characters, ends with #Shorts")
    hook: str = Field(description="Spoken before scene 1's narration (don't repeat it there); stops the scroll honestly")
    scenes: list[Scene] = Field(description="6-9 scenes, about 40-55 seconds spoken in total")
    verse_quote: str = Field(description="The provided translation, copied exactly")
    description: str = Field(description="YouTube description: 2-3 short paragraphs and the verse reference")
    tags: list[str] = Field(description="8-15 YouTube tags, no # symbol")


class Issue(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    problem: str
    fix: str


class Review(BaseModel):
    fidelity_score: int = Field(description="0-10 faithfulness to the verse and traditional meaning")
    respect_score: int = Field(description="0-10 reverence and depiction rules")
    quote_is_exact: bool
    issues: list[Issue]
    verdict: Literal["approve", "revise", "reject"]


# ----------------------------------------------------------------------------------------
# Claude
# ----------------------------------------------------------------------------------------
_client: anthropic.Anthropic | None = None


def ask(system: str, prompt: str, schema: type[BaseModel], effort: str = "high") -> BaseModel:
    global _client
    _client = _client or anthropic.Anthropic(max_retries=4)   # reads ANTHROPIC_API_KEY
    resp = _client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        output_config={"effort": effort},
        fallbacks="default",                         # re-run on a fallback model if declined
        betas=["server-side-fallback-2026-07-01"],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request; try another verse.")
    if resp.parsed_output is None:
        raise RuntimeError(f"No structured output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


def verse_block(v: dict) -> str:
    return (f"Reference: BG {v['chapter']}.{v['verse']}\nSanskrit: {v['sanskrit']}\n"
            f"Transliteration: {v.get('transliteration', '')}\n"
            f"Translation (quote exactly): {v['translation']}\nSource: {v['source']}")


def normalise(s: str) -> str:
    return " ".join(s.strip().strip("\"'“”‘’").split())


def write_and_review(v: dict, angle: str | None) -> tuple[Short, Review]:
    writer = (f"You write YouTube Shorts for '{CHANNEL_NAME}', a Bhagavad Gita channel for modern seekers.\n"
              f"Connect one verse to one concrete, relatable modern situation. Hook in the first 2 seconds, "
              f"quote the verse, explain it simply, end with a reflection question.\n{GUARDRAILS}")
    reviewer = ("You are a strict scripture-fidelity reviewer who did not write this script. Check the quote is "
                "exact, the meaning is faithful, nothing modern is presented as scripture, and the tone and image "
                "prompts are respectful. Approve only with no blocker/major issues.\n" + GUARDRAILS)
    prompt = verse_block(v) + (f"\nAngle: {angle}" if angle else "\nPick the most resonant modern angle.")
    review = None
    for _ in range(3):
        if review:
            prompt += "\n\nFix these reviewer issues:\n" + "\n".join(
                f"- [{i.severity}] {i.problem} -> {i.fix}" for i in review.issues)
        short = ask(writer, prompt, Short)
        review = ask(reviewer, f"{verse_block(v)}\n\nScript:\n{short.model_dump_json(indent=1)}", Review)
        if normalise(short.verse_quote) != normalise(v["translation"]):
            review.quote_is_exact, review.verdict = False, "revise"
            review.issues.append(Issue(severity="blocker", problem="verse_quote differs from the translation",
                                       fix="copy the translation exactly"))
        if review.verdict != "revise":
            break
    return short, review


# ----------------------------------------------------------------------------------------
# Verses
# ----------------------------------------------------------------------------------------
def pick_verse(history: list[str], ref: str | None) -> dict:
    if not VERSES_FILE.exists():
        sys.exit(f"{VERSES_FILE} not found. Add a translation you have rights to (see top of this file).")
    verses = json.loads(VERSES_FILE.read_text(encoding="utf-8"))
    if ref:
        c, n = map(int, ref.split("."))
        match = [v for v in verses if v["chapter"] == c and v["verse"] == n]
        if not match:
            sys.exit(f"BG {ref} is not in {VERSES_FILE}")
        return match[0]
    fresh = [v for v in verses if f"{v['chapter']}.{v['verse']}" not in history[-120:]]
    return random.choice(fresh or verses)


# ----------------------------------------------------------------------------------------
# Rendering: Pillow frames + edge-tts voice + ffmpeg
# ----------------------------------------------------------------------------------------
def font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = (["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"] if bold else
             ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"])
    dirs = ["/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental",
            "C:/Windows/Fonts", "."]
    for d in dirs:
        for n in names:
            if Path(d, n).exists():
                return ImageFont.truetype(str(Path(d, n)), size)
    return ImageFont.load_default(size)


def background(i: int, folder: Path):
    """Use images/scene_<i>.(png|jpg) if you generated one from the prompt, else a warm gradient."""
    from PIL import Image, ImageFilter
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = folder / "images" / f"scene_{i}.{ext}"
        if p.exists():
            img = Image.open(p).convert("RGB")
            scale = max(W * 1.15 / img.width, H * 1.15 / img.height)   # headroom for the slow zoom
            img = img.resize((int(img.width * scale), int(img.height * scale)))
            left, top = (img.width - int(W * 1.15)) // 2, (img.height - int(H * 1.15)) // 2
            return img.crop((left, top, left + int(W * 1.15), top + int(H * 1.15)))
    palettes = [((255, 153, 51), (60, 20, 90)), ((250, 200, 90), (20, 40, 90)), ((230, 110, 60), (25, 25, 60))]
    top, bottom = palettes[i % len(palettes)]
    w, h = int(W * 1.15), int(H * 1.15)
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / h
        grad.putpixel((0, y), tuple(int(top[k] * (1 - t) + bottom[k] * t) for k in range(3)))
    return grad.resize((w, h)).filter(ImageFilter.GaussianBlur(2))


def draw_frame(i: int, scene: Scene, ref: str, folder: Path, path: Path) -> None:
    from PIL import Image, ImageDraw
    img = background(i, folder).convert("RGBA")
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    for y in range(img.height // 2, img.height):                       # darken the lower half for legibility
        d.line([(0, y), (img.width, y)], fill=(0, 0, 0, int(200 * (y - img.height / 2) / (img.height / 2))))
    img = Image.alpha_composite(img, shade)
    d = ImageDraw.Draw(img)
    ox, oy = (img.width - W) // 2, (img.height - H) // 2                # draw inside the centre crop
    f_big, f_small = font(64, bold=True), font(36)
    lines = textwrap.wrap(scene.on_screen_text, width=22)
    y = oy + int(H * 0.62)
    for line in lines:
        tw = d.textlength(line, font=f_big)
        d.text((ox + (W - tw) / 2, y), line, font=f_big, fill="white", stroke_width=3, stroke_fill="black")
        y += 82
    d.text((ox + 60, oy + 90), f"{CHANNEL_NAME} · {ref}", font=f_small, fill=(255, 235, 200))
    d.text((ox + 60, oy + H - 110), "AI-generated", font=font(28), fill=(220, 220, 220))
    img.convert("RGB").save(path)


async def tts(text: str, path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate="-5%").save(str(path))


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def render(short: Short, ref: str, folder: Path) -> Path:
    work = folder / "work"
    work.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, scene in enumerate(short.scenes):
        frame, voice, clip = work / f"f{i}.png", work / f"v{i}.mp3", work / f"c{i}.mp4"
        draw_frame(i, scene, ref, folder, frame)
        text = (short.hook + " " + scene.narration) if i == 0 else scene.narration
        try:
            asyncio.run(tts(text, voice))
            secs = duration(voice) + 0.35
            audio = ["-i", str(voice)]
        except Exception as e:  # no network / edge-tts missing: silent scene sized to reading time
            print(f"  voice-over skipped for scene {i}: {e}")
            secs, audio = max(2.5, len(text.split()) / 2.6), ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        frames = int(secs * FPS)
        zoom = f"zoompan=z='1+0.08*on/{frames}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS}"
        ffmpeg("-i", str(frame), *audio, "-filter_complex", f"[0:v]{zoom},format=yuv420p[v]",
               "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-t", f"{secs:.2f}", str(clip))
        clips.append(clip)
    listing = work / "clips.txt"
    listing.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips))
    joined, final = work / "joined.mp4", folder / "short.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined))
    if MUSIC_FILE.exists():
        ffmpeg("-i", str(joined), "-stream_loop", "-1", "-i", str(MUSIC_FILE), "-filter_complex",
               "[1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first[a]", "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", str(final))
    else:
        joined.replace(final)
    return final


# ----------------------------------------------------------------------------------------
# YouTube upload (YouTube Data API v3)
# ----------------------------------------------------------------------------------------
def youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), scopes) if TOKEN_FILE.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            sys.exit(f"{CLIENT_SECRET} missing: create an OAuth 'Desktop app' client in Google Cloud Console.")
        creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), scopes).run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(folder: Path, privacy: str, publish_at: str | None) -> str:
    from googleapiclient.http import MediaFileUpload
    meta = json.loads((folder / "short.json").read_text(encoding="utf-8"))
    if meta["review"]["verdict"] != "approve":
        sys.exit("This Short did not pass review; not uploading.")
    status = {"privacyStatus": "private" if publish_at else privacy,
              "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": True}           # YouTube's "altered or synthetic content" label
    if publish_at:
        status["publishAt"] = publish_at                # e.g. 2026-10-09T01:00:00Z, goes public then
    body = {"snippet": {"title": meta["short"]["title"][:100], "description": meta["youtube_description"],
                        "tags": meta["short"]["tags"], "categoryId": "27"},  # 27 = Education
            "status": status}
    req = youtube().videos().insert(part="snippet,status", body=body,
                                    media_body=MediaFileUpload(str(folder / "short.mp4"), resumable=True))
    resp = None
    while resp is None:
        progress, resp = req.next_chunk()
        if progress:
            print(f"  uploaded {int(progress.progress() * 100)}%")
    meta["youtube_id"] = resp["id"]
    (folder / "short.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    url = f"https://youtube.com/shorts/{resp['id']}"
    print(f"Uploaded ({status['privacyStatus']}): {url}")
    return url


# ----------------------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------------------
def make(ref: str | None, angle: str | None) -> Path | None:
    OUT_DIR.mkdir(exist_ok=True)
    hist_file = OUT_DIR / "history.json"
    history = json.loads(hist_file.read_text()) if hist_file.exists() else []
    v = pick_verse(history, ref)
    ref_s = f"BG {v['chapter']}.{v['verse']}"
    print(f"Writing a Short for {ref_s} ...")
    short, review = write_and_review(v, angle)
    folder = OUT_DIR / f"{datetime.now():%Y%m%d-%H%M}-{v['chapter']}-{v['verse']}"
    folder.mkdir(parents=True)
    disclosure = AI_DISCLOSURE.format(source=v["source"])
    meta = {"verse": v, "short": short.model_dump(), "review": review.model_dump(),
            "youtube_description": f"{short.description}\n\n{ref_s}\n{disclosure}\n\n#Shorts #BhagavadGita"}
    (folder / "short.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "image_prompts.md").write_text("\n\n".join(
        f"scene_{i}: {s.image_prompt}" for i, s in enumerate(short.scenes)), encoding="utf-8")

    ok = (review.verdict == "approve" and review.quote_is_exact
          and min(review.fidelity_score, review.respect_score) >= MIN_SCORE)
    print(f"Review: {review.verdict} (fidelity {review.fidelity_score}, respect {review.respect_score})")
    for i in review.issues:
        print(f"  [{i.severity}] {i.problem}")
    if not ok:
        print(f"Not rendered. Script saved in {folder} for you to read.")
        return None
    print("Rendering video ...")
    video = render(short, ref_s, folder)
    history.append(f"{v['chapter']}.{v['verse']}")
    hist_file.write_text(json.dumps(history))
    print(f"Done: {video}\nTitle: {short.title}\n"
          f"Tip: generate images from {folder / 'image_prompts.md'}, save them as {folder}/images/scene_N.png, "
          f"then run `python {Path(__file__).name} rerender {folder}`.")
    return folder


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="write, review and render one Short")
    m.add_argument("--verse", help="e.g. 2.47 (default: a random unused verse)")
    m.add_argument("--angle", help="e.g. 'exam results anxiety'")
    r = sub.add_parser("rerender", help="re-render after adding your own images/scene_N.png")
    r.add_argument("folder", type=Path)
    u = sub.add_parser("upload", help="upload a rendered Short")
    u.add_argument("folder", type=Path)
    u.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    u.add_argument("--publish-at", help="ISO time to auto-publish, e.g. 2026-10-09T01:00:00Z")
    a = sub.add_parser("auto", help="make + upload (private, or scheduled with --publish-at)")
    a.add_argument("--publish-at")
    args = p.parse_args()

    if args.cmd == "make":
        make(args.verse, args.angle)
    elif args.cmd == "rerender":
        meta = json.loads((args.folder / "short.json").read_text(encoding="utf-8"))
        v = meta["verse"]
        print(render(Short.model_validate(meta["short"]), f"BG {v['chapter']}.{v['verse']}", args.folder))
    elif args.cmd == "upload":
        upload(args.folder, args.privacy, args.publish_at)
    elif args.cmd == "auto":
        folder = make(None, None)
        if folder:
            upload(folder, "private", args.publish_at)


if __name__ == "__main__":
    main()
