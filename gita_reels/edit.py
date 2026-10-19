"""Edit and export: conform clips, build stems and titles, render the master, and write editable timelines.

Every reel folder is a self-contained editing project:
    clips_raw/        untouched model output
    clips/            conformed shots (1080x1920, 24 fps, trimmed, graded) used on the timeline
    titles/           transparent PNG layers (verse line, channel brand)
    audio/            music.wav, ambience.wav, voice.wav, mix.wav stems
    captions.srt      verse line and narration with timings
    timeline.otio     OpenTimelineIO (DaVinci Resolve, Premiere via OTIO, Nuke, etc.)
    timeline.xml      FCP 7 XML (File > Import in Premiere Pro and DaVinci Resolve)
    final.mp4         the flattened master that gets published
"""
from __future__ import annotations

import asyncio
import json
import random
import subprocess
import textwrap
from pathlib import Path

from .produce import ShotResult
from .settings import Settings
from .writer import ReelPlan

# Light touch: the generated footage already carries the look; this only unifies shots.
GRADE = "eq=contrast=1.06:saturation=0.96,colorbalance=rh=0.03:bh=-0.03:bs=0.02,noise=alls=4:allf=t"
FLASH_FRAMES = 4


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return bool(out.strip())


def font(size: int, serif: bool = True):
    from PIL import ImageFont
    names = (["DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "Georgia.ttf", "georgia.ttf", "Times New Roman.ttf"]
             if serif else ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"])
    for d in ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation", "/Library/Fonts",
              "/System/Library/Fonts/Supplemental", "C:/Windows/Fonts", "."]:
        for n in names:
            if Path(d, n).exists():
                return ImageFont.truetype(str(Path(d, n)), size)
    return ImageFont.load_default(size)


# --- titles ---------------------------------------------------------------------------------
def verse_title(text: str, ref: str, w: int, h: int, path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFilter
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    lines = textwrap.wrap(f"“{text}”", width=24)
    f_main, f_ref = font(60), font(36)
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ds, d = ImageDraw.Draw(shadow), ImageDraw.Draw(layer)
    y = int(h * 0.60)
    for line in lines + [f"— Bhagavad Gita {ref}"]:
        f = f_ref if line.startswith("— ") else f_main
        tw = d.textlength(line, font=f)
        for draw, fill in ((ds, (0, 0, 0, 200)), (d, (255, 244, 225, 255))):
            draw.text(((w - tw) / 2, y), line, font=f, fill=fill)
        y += 78 if f is f_main else 56
    layer = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)), layer)
    layer.save(path)
    return path


def brand_layer(handle: str, w: int, h: int, path: Path) -> Path:
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    small = font(30, serif=False)
    if handle:
        tw = d.textlength(handle, font=small)
        d.text((w - tw - 48, 150), handle, font=small, fill=(255, 255, 255, 210))
    d.text((48, h - 120), "AI-generated", font=font(26, serif=False), fill=(235, 235, 235, 190))
    layer.save(path)
    return path


# --- conform --------------------------------------------------------------------------------
def conform(result: ShotResult, secs: float, framing: str, flash: bool, s: Settings, folder: Path) -> tuple[Path, Path]:
    r = s.reel
    src = result.clip
    offset = max(0.0, min(0.5, duration(src) - secs))      # skip the keyframe-still first instant
    vf = (["transpose=1"] if framing == "rotated_wide" else []) + [
        f"scale={r.width}:{r.height}:force_original_aspect_ratio=increase", f"crop={r.width}:{r.height}",
        "setsar=1", f"fps={r.fps}", f"tpad=stop_mode=clone:stop_duration={secs:.2f}", GRADE]
    if flash:
        vf.append(f"fade=t=in:st=0:d={FLASH_FRAMES / r.fps:.3f}:color=white")
    vf.append("format=yuv420p")
    video = folder / "clips" / f"shot_{result.index + 1:02d}.mp4"
    audio = folder / "audio" / "parts" / f"shot_{result.index + 1:02d}.wav"
    video.parent.mkdir(parents=True, exist_ok=True)
    audio.parent.mkdir(parents=True, exist_ok=True)
    ff("-ss", f"{offset:.3f}", "-i", str(src), "-t", f"{secs:.3f}", "-vf", ",".join(vf), "-an",
       "-c:v", "libx264", "-preset", s.reel.preset, "-crf", "16", str(video))
    if has_audio(src):
        ff("-ss", f"{offset:.3f}", "-i", str(src), "-t", f"{secs:.3f}", "-vn", "-af", "apad",
           "-ac", "2", "-ar", "48000", "-t", f"{secs:.3f}", str(audio))
    else:
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{secs:.3f}", str(audio))
    return video, audio


# --- audio ----------------------------------------------------------------------------------
def pick_music(s: Settings, mood: str) -> Path | None:
    tracks = [p for p in Path(s.paths.music).glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac")]
    matching = [p for p in tracks if mood in p.stem.lower()]
    return random.choice(matching or tracks) if tracks else None


def music_credit(track: Path | None) -> str:
    """Optional sidecar `<track>.txt` holds the licence credit line for the description."""
    if track and track.with_suffix(".txt").exists():
        return track.with_suffix(".txt").read_text(encoding="utf-8").strip()
    return ""


def build_music(track: Path | None, total: float, path: Path) -> None:
    fade = f"afade=t=in:d=0.3,afade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5"
    if track:
        ff("-stream_loop", "-1", "-i", str(track), "-t", f"{total:.3f}", "-af", fade, "-ac", "2", "-ar", "48000",
           str(path))
    else:  # fallback bed: low drone with a slow swell. Add licensed tracks to music/ for real releases.
        ff("-f", "lavfi", "-i", f"aevalsrc=0.5*sin(2*PI*55*t)*(0.6+0.4*sin(2*PI*0.25*t))+0.2*sin(2*PI*82.5*t)"
                                f":s=48000:d={total:.3f}", "-af", f"lowpass=f=320,{fade}", "-ac", "2", str(path))


async def _tts(text: str, voice: str, path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate="-6%").save(str(path))


def build_voice(plan: ReelPlan, starts: list[float], total: float, s: Settings, folder: Path) -> Path | None:
    lines = [(i, sh.narration) for i, sh in enumerate(plan.shots) if sh.narration.strip()]
    if not (s.reel.narration and lines):
        return None
    parts, filters = [], []
    for k, (i, text) in enumerate(lines):
        p = folder / "audio" / "parts" / f"vo_{i + 1:02d}.mp3"
        asyncio.run(_tts(text, s.reel.voice, p))
        parts += ["-i", str(p)]
        filters.append(f"[{k}:a]adelay={int(starts[i] * 1000)}|{int(starts[i] * 1000)}[v{k}]")
    mix = "".join(f"[v{k}]" for k in range(len(lines)))
    out = folder / "audio" / "voice.wav"
    ff(*parts, "-filter_complex", ";".join(filters) + f";{mix}amix=inputs={len(lines)}:normalize=0,"
       f"apad[a]", "-map", "[a]", "-t", f"{total:.3f}", "-ac", "2", "-ar", "48000", str(out))
    return out


def mix(music: Path, ambience: Path, voice: Path | None, path: Path) -> None:
    if voice:
        graph = ("[0:a]volume=0.9[m];[2:a]asplit[v][vk];[m][vk]sidechaincompress=threshold=0.05:ratio=6:"
                 "attack=20:release=300[md];[1:a]volume=0.35[amb];[md][amb][v]amix=inputs=3:normalize=0,"
                 "loudnorm=I=-14:TP=-1.5:LRA=7[a]")
        inputs = ["-i", str(music), "-i", str(ambience), "-i", str(voice)]
    else:
        graph = "[0:a]volume=0.9[m];[1:a]volume=0.35[amb];[m][amb]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=7[a]"
        inputs = ["-i", str(music), "-i", str(ambience)]
    ff(*inputs, "-filter_complex", graph, "-map", "[a]", "-ar", "48000", "-ac", "2", str(path))


# --- captions & timeline --------------------------------------------------------------------
def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(entries: list[tuple[float, float, str]], path: Path) -> None:
    path.write_text("".join(f"{n}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n" for n, (a, b, t) in enumerate(entries, 1)),
                    encoding="utf-8")


def write_timeline(name: str, fps: int, shots: list[tuple[Path, float]], titles: list[tuple[Path, float, float]],
                   brand: Path, stems: dict[str, Path], total: float, folder: Path) -> None:
    import opentimelineio as otio
    RT, TR = otio.opentime.RationalTime, otio.opentime.TimeRange

    def clip(path: Path, secs: float, label: str) -> otio.schema.Clip:
        frames = round(secs * fps)
        rng = TR(RT(0, fps), RT(frames, fps))
        return otio.schema.Clip(name=label, source_range=rng, media_reference=otio.schema.ExternalReference(
            target_url=path.resolve().as_uri(), available_range=rng))

    def gap(secs: float) -> otio.schema.Gap:
        return otio.schema.Gap(source_range=TR(RT(0, fps), RT(round(secs * fps), fps)))

    tl = otio.schema.Timeline(name=name)
    v1 = otio.schema.Track(name="V1 shots", kind=otio.schema.TrackKind.Video)
    for path, secs in shots:
        v1.append(clip(path, secs, path.stem))
    v2 = otio.schema.Track(name="V2 titles", kind=otio.schema.TrackKind.Video)
    cursor = 0.0
    for path, start, end in titles:
        if start > cursor:
            v2.append(gap(start - cursor))
        v2.append(clip(path, end - start, path.stem))
        cursor = end
    v3 = otio.schema.Track(name="V3 brand", kind=otio.schema.TrackKind.Video)
    v3.append(clip(brand, total, "brand"))
    tl.tracks.extend([v1, v2, v3])
    for label, path in stems.items():
        a = otio.schema.Track(name=label, kind=otio.schema.TrackKind.Audio)
        a.append(clip(path, total, path.stem))
        tl.tracks.append(a)
    otio.adapters.write_to_file(tl, str(folder / "timeline.otio"))
    xml_path = folder / "timeline.xml"
    otio.adapters.write_to_file(tl, str(xml_path), adapter_name="fcp_xml")
    _set_sequence_format(xml_path, fps, *_frame_size(shots[0][0]))


def _frame_size(video: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def _set_sequence_format(xml_path: Path, fps: int, w: int, h: int) -> None:
    """OTIO leaves the sequence format empty; without it Premiere opens a vertical edit as 16:9."""
    import xml.etree.ElementTree as ET
    tree = ET.parse(xml_path)
    for fmt in tree.getroot().iter("format"):
        fmt.clear()
        sc = ET.SubElement(fmt, "samplecharacteristics")
        rate = ET.SubElement(sc, "rate")
        ET.SubElement(rate, "timebase").text = str(fps)
        ET.SubElement(rate, "ntsc").text = "FALSE"
        for tag, val in (("width", w), ("height", h), ("pixelaspectratio", "square"), ("fielddominance", "none")):
            ET.SubElement(sc, tag).text = str(val)
    tree.write(xml_path, encoding="UTF-8", xml_declaration=True)


# --- the whole edit -------------------------------------------------------------------------
def edit(plan: ReelPlan, results: list[ShotResult], verse: dict | None, s: Settings, folder: Path) -> dict:
    r = s.reel
    conformed, starts, t = [], [], 0.0
    for res in results:
        shot = plan.shots[res.index]
        starts.append(t)
        conformed.append(conform(res, shot.on_screen_seconds, shot.framing, shot.transition_in == "flash", s, folder))
        t += shot.on_screen_seconds
    total = t

    listing = folder / "clips" / "list.txt"
    listing.write_text("".join(f"file '{v.resolve().as_posix()}'\n" for v, _ in conformed))
    picture = folder / "clips" / "_picture.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(picture))

    audio = folder / "audio"
    alist = audio / "parts" / "list.txt"
    alist.write_text("".join(f"file '{a.resolve().as_posix()}'\n" for _, a in conformed))
    ambience = audio / "ambience.wav"
    ff("-f", "concat", "-safe", "0", "-i", str(alist), "-c", "copy", str(ambience))
    track = pick_music(s, plan.music_mood)
    music = audio / "music.wav"
    build_music(track, total, music)
    voice = build_voice(plan, starts, total, s, folder)
    mixed = audio / "mix.wav"
    mix(music, ambience, voice, mixed)

    titles_dir = folder / "titles"
    titles_dir.mkdir(exist_ok=True)
    brand = brand_layer(s.channel.handle, r.width, r.height, titles_dir / "brand.png")
    titles, srt = [], []
    if verse and plan.verse_line and 0 <= plan.verse_shot_index < len(plan.shots):
        i = plan.verse_shot_index
        a, b = starts[i], min(total, starts[i] + max(plan.shots[i].on_screen_seconds, 2.5))
        ref = f"{verse['chapter']}.{verse['verse']}"
        titles.append((verse_title(plan.verse_line, ref, r.width, r.height, titles_dir / "verse.png"), a, b))
        srt.append((a, b, f"“{plan.verse_line}” — Bhagavad Gita {ref}"))
    for i, sh in enumerate(plan.shots):
        if r.narration and sh.narration.strip():
            srt.append((starts[i], starts[i] + sh.on_screen_seconds, sh.narration))
    write_srt(sorted(srt), folder / "captions.srt")

    # flatten: picture + titles + brand + mix
    inputs, chain, last = ["-i", str(picture)], [], "0:v"
    for k, (png, a, b) in enumerate(titles, start=1):
        inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", str(png)]
        chain.append(f"[{last}][{k}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[o{k}]")
        last = f"o{k}"
    k = len(titles) + 1
    inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", str(brand), "-i", str(mixed)]
    chain.append(f"[{last}][{k}:v]overlay=0:0,format=yuv420p[v]")
    final = folder / "final.mp4"
    ff(*inputs, "-filter_complex", ";".join(chain), "-map", "[v]", "-map", f"{k + 1}:a",
       "-c:v", "libx264", "-preset", r.preset, "-crf", "17", "-r", str(r.fps), "-c:a", "aac", "-b:a", "192k",
       "-t", f"{total:.3f}", "-movflags", "+faststart", str(final))
    picture.unlink(missing_ok=True)

    stems = {"A1 music": music, "A2 ambience": ambience}
    if voice:
        stems["A3 voice"] = voice
    write_timeline(plan.title, r.fps, [(v, plan.shots[i].on_screen_seconds) for i, (v, _) in enumerate(conformed)],
                   titles, brand, stems, total, folder)
    return {"final": str(final), "seconds": round(total, 2), "music_track": str(track) if track else None,
            "music_credit": music_credit(track)}
