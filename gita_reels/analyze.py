"""Reference video analysis: shot boundaries, per-shot measurements, a contact sheet and a cinematography
profile the planner can use.

Measured (ffmpeg + numpy, no AI): duration, fps, resolution, codec; scene cuts; per shot: duration, frames at
start/middle/end, luma percentiles, saturation, warm share, dominant colours, global camera motion
(pan/tilt from phase correlation, push/pull from scale search), motion energy, a sharpness map
(centre vs edges) as a depth-of-field cue, the brightest region (light source / backlight cue) and the
centroid of detail (approximate subject position).

Inferred (Claude vision, optional): shot type, angle, plausible focal length, lighting setup, what makes it
cinematic. Lens, aperture and camera are estimates from rendered pixels, never recovered metadata.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

AW = 160          # analysis width in pixels


def probe(path: Path) -> dict:
    out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                     str(path)], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    a = next((s for s in out["streams"] if s["codec_type"] == "audio"), None)
    num, den = (int(x) for x in v["avg_frame_rate"].split("/"))
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", 0) or 0)
    if abs(rot) == 90:
        w, h = h, w
    return {"duration": float(out["format"]["duration"]), "fps": round(num / den, 3) if den else 0, "width": w,
            "height": h, "aspect": f"{w}:{h}", "video_codec": v["codec_name"], "pix_fmt": v.get("pix_fmt"),
            "audio_codec": a["codec_name"] if a else None, "bit_rate": int(out["format"].get("bit_rate", 0))}


def frames(path: Path, fps: float, width: int = AW) -> np.ndarray:
    """All frames at analysis size as float RGB, shape (n, h, w, 3)."""
    info = probe(path)
    h = int(round(info["height"] * width / info["width"] / 2) * 2)
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", f"scale={width}:{h},format=rgb24",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    n = len(raw) // (width * h * 3)
    return np.frombuffer(raw, np.uint8)[: n * width * h * 3].reshape(n, h, width, 3).astype(np.float32)


def cuts(path: Path, threshold: float = 0.3) -> list[float]:
    log = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf",
                          f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return [float(t) for t in re.findall(r"pts_time:([\d.]+)", log)]


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """Translation from a to b by phase correlation (pixels) and the peak strength (confidence)."""
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2((a - a.mean()) * win), np.fft.fft2((b - b.mean()) * win)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.real(np.fft.ifft2(r))
    y, x = np.unravel_index(np.argmax(c), c.shape)
    if y > a.shape[0] / 2:
        y -= a.shape[0]
    if x > a.shape[1] / 2:
        x -= a.shape[1]
    return float(-x), float(-y), float(c.max())


def _scale(a: np.ndarray, b: np.ndarray) -> float:
    """Best zoom factor taking a to b (centre crop/scale search): >1 means the image grows (push in / zoom)."""
    from PIL import Image
    h, w = a.shape
    best, best_err = 1.0, None
    ia = Image.fromarray(a.astype(np.uint8))
    for s in np.linspace(0.92, 1.08, 17):
        cw, ch = int(w / s), int(h / s)
        if cw > w or ch > h:   # zoom out: compare b's centre with a scaled-down a
            small = np.asarray(ia.resize((int(w * s), int(h * s))), np.float32)
            y0, x0 = (h - small.shape[0]) // 2, (w - small.shape[1]) // 2
            err = np.abs(b[y0:y0 + small.shape[0], x0:x0 + small.shape[1]] - small).mean()
        else:
            y0, x0 = (h - ch) // 2, (w - cw) // 2
            crop = ia.crop((x0, y0, x0 + cw, y0 + ch)).resize((w, h))
            err = np.abs(np.asarray(crop, np.float32) - b).mean()
        if best_err is None or err < best_err:
            best, best_err = float(s), err
    return best


def _sharpness(g: np.ndarray) -> np.ndarray:
    lap = np.abs(4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:])
    return lap


def _palette(rgb: np.ndarray, k: int = 6) -> list[str]:
    from PIL import Image
    img = Image.fromarray(rgb.astype(np.uint8)).quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    pal = img.getpalette()[: k * 3]
    counts = sorted(img.getcolors(), reverse=True)
    return ["#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]) for _, i in counts[:k]]


def _where(y: float, x: float) -> str:
    v = "top" if y < 0.34 else ("bottom" if y > 0.66 else "middle")
    hz = "left" if x < 0.34 else ("right" if x > 0.66 else "centre")
    return f"{v}-{hz}"


def measure_shot(fr: np.ndarray, fps: float) -> dict:
    lum = _luma(fr)
    mid = fr[len(fr) // 2]
    ml = lum[len(fr) // 2]
    mx, mn = fr.max(axis=-1), fr.min(axis=-1)
    sat = float(np.where(mx > 0, (mx - mn) / (mx + 1e-6), 0).mean())
    warm = float((fr[..., 0] > fr[..., 2] + 8).mean())
    # camera motion over the shot (start -> end), in fractions of frame size per second
    step = max(1, len(fr) // 8)
    dx = dy = 0.0
    scales = []
    conf = []
    for i in range(0, len(fr) - step, step):
        x, y, c = _shift(lum[i], lum[i + step])
        dx, dy = dx + x, dy + y
        conf.append(c)
        scales.append(_scale(lum[i], lum[i + step]))
    secs = len(fr) / fps
    h, w = ml.shape
    pan = dx / w / secs if secs else 0
    tilt = dy / h / secs if secs else 0
    zoom = float(np.prod(scales)) if scales else 1.0
    zoom_rate = (zoom - 1) / secs if secs else 0
    moves = []
    if abs(pan) > 0.02:
        moves.append(f"{'pan/track right' if pan > 0 else 'pan/track left'} ({abs(pan) * 100:.0f}% of width/s)")
    if abs(tilt) > 0.02:
        moves.append(f"{'tilt/crane down' if tilt > 0 else 'tilt/crane up'} ({abs(tilt) * 100:.0f}% of height/s)")
    if abs(zoom_rate) > 0.01:
        moves.append(f"{'push-in or zoom-in' if zoom_rate > 0 else 'pull-back or zoom-out'} "
                     f"({abs(zoom_rate) * 100:.1f}% scale/s)")
    energy = float(np.abs(np.diff(lum, axis=0)).mean()) if len(fr) > 1 else 0.0
    sharp = _sharpness(ml)
    cy, cx = sharp.shape[0] // 4, sharp.shape[1] // 4
    centre = float(sharp[cy:-cy, cx:-cx].mean())
    edges = float(np.concatenate([sharp[:cy].ravel(), sharp[-cy:].ravel()]).mean()) + 1e-6
    ratio = centre / edges
    yy, xx = np.mgrid[0:sharp.shape[0], 0:sharp.shape[1]]
    wts = sharp ** 2
    sy, sx = float((yy * wts).sum() / wts.sum() / sharp.shape[0]), float((xx * wts).sum() / wts.sum() / sharp.shape[1])
    blur = np.asarray(ml)
    bright = np.unravel_index(np.argmax(_box(blur, 9)), blur.shape)
    by, bx = bright[0] / h, bright[1] / w
    top_vs_bottom = float(ml[: h // 3].mean() - ml[-h // 3:].mean())
    return {
        "luma_p5": round(float(np.percentile(ml, 5)), 1), "luma_p50": round(float(np.percentile(ml, 50)), 1),
        "luma_p95": round(float(np.percentile(ml, 95)), 1), "saturation": round(sat, 3),
        "warm_share": round(warm, 3), "palette": _palette(mid),
        "camera_motion": moves or ["static / locked-off (or motion too subtle to measure)"],
        "pan_per_s": round(pan, 4), "tilt_per_s": round(tilt, 4), "scale_per_s": round(zoom_rate, 4),
        "motion_confidence": round(float(np.mean(conf)) if conf else 0.0, 3),
        "motion_energy": round(energy, 2),
        "dof_cue": ("shallow (sharp centre, soft edges)" if ratio > 2.2 else
                    "moderate" if ratio > 1.3 else "deep or uniform focus"),
        "sharp_centre_to_edge": round(ratio, 2),
        "detail_centroid": _where(sy, sx),
        "brightest_region": _where(by, bx),
        "light_cue": ("bright source in frame / backlit" if ml.max() > 235 and by < 0.6 else
                      "sky brighter than ground" if top_vs_bottom > 25 else "even or low-key"),
    }


def _box(a: np.ndarray, k: int) -> np.ndarray:
    c = np.cumsum(np.cumsum(np.pad(a, ((1, 0), (1, 0))), 0), 1)
    out = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    return np.pad(out, ((k // 2, a.shape[0] - out.shape[0] - k // 2), (k // 2, a.shape[1] - out.shape[1] - k // 2)))


def extract_still(path: Path, t: float, out: Path, height: int = 640) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                    "-vf", f"scale=-2:{height}", str(out)], check=True)
    return out


def contact_sheet(stills: list[tuple[Path, str]], out: Path, cols: int = 6) -> Path:
    from PIL import Image, ImageDraw
    from .edit import font
    imgs = [(Image.open(p).convert("RGB"), label) for p, label in stills]
    tw = 240
    th = int(imgs[0][0].height * tw / imgs[0][0].width)
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 8) + 8, rows * (th + 34) + 8), (18, 18, 18))
    d = ImageDraw.Draw(sheet)
    f = font(16, serif=False)
    for k, (img, label) in enumerate(imgs):
        x, y = 8 + (k % cols) * (tw + 8), 8 + (k // cols) * (th + 34)
        sheet.paste(img.resize((tw, th)), (x, y))
        d.text((x, y + th + 6), label, font=f, fill=(230, 230, 230))
    sheet.save(out, quality=90)
    return out


def analyze(path: Path, out_dir: Path, threshold: float = 0.3, min_shot: float = 0.4, claude=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(path)
    fr = frames(path, info["fps"])
    fps = info["fps"] or 24
    bounds = [0.0] + [t for t in cuts(path, threshold) if t > min_shot] + [info["duration"]]
    merged = [bounds[0]]
    for t in bounds[1:]:
        if t - merged[-1] >= min_shot or t == bounds[-1]:
            merged.append(t)
    if len(merged) > 2 and merged[-1] - merged[-2] < min_shot:
        merged.pop(-2)
    shots, stills = [], []
    (out_dir / "frames").mkdir(exist_ok=True)
    for k, (a, b) in enumerate(zip(merged, merged[1:]), start=1):
        seg = fr[int(a * fps): max(int(a * fps) + 1, int(b * fps))]
        m = measure_shot(seg, fps)
        paths = []
        for tag, t in (("start", a + 0.05), ("mid", (a + b) / 2), ("end", max(a, b - 0.08))):
            p = extract_still(path, t, out_dir / "frames" / f"shot{k:02d}_{tag}.jpg")
            paths.append(p.name)
            stills.append((p, f"#{k} {tag} {t:.2f}s"))
        shots.append({"shot": k, "start": round(a, 3), "end": round(b, 3), "seconds": round(b - a, 3),
                      "frames": paths, **m})
    sheet = contact_sheet(stills, out_dir / "contact_sheet.jpg")
    durs = [s["seconds"] for s in shots]
    report = {
        "source": path.name, **info, "shots": shots, "shot_count": len(shots),
        "avg_shot_seconds": round(float(np.mean(durs)), 2),
        "shot_seconds_range": [round(min(durs), 2), round(max(durs), 2)],
        "cuts_per_second": round(len(shots) / info["duration"], 2),
        "luma_p5": round(float(np.mean([s["luma_p5"] for s in shots])), 1),
        "luma_p95": round(float(np.mean([s["luma_p95"] for s in shots])), 1),
        "saturation": round(float(np.mean([s["saturation"] for s in shots])), 3),
        "warm_share": round(float(np.mean([s["warm_share"] for s in shots])), 3),
        "contact_sheet": sheet.name,
        "notes": ["Lens, aperture and camera are not recoverable from rendered footage; anything below labelled "
                  "'estimate' is a cinematographic inference."],
    }
    if audio := _loudness(path):
        report["audio"] = audio
    if claude is not None:
        report["cinematography"] = claude_reading(claude, out_dir, report)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(to_markdown(report), encoding="utf-8")
    return report


def _loudness(path: Path) -> dict | None:
    log = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-vn", "-f",
                          "null", "-"], capture_output=True, text=True).stderr
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    lra = re.findall(r"LRA:\s+(-?[\d.]+) LU", log)
    return {"integrated_lufs": float(i[-1]), "loudness_range_lu": float(lra[-1]) if lra else None} if i else None


def claude_reading(claude, out_dir: Path, report: dict) -> list[dict]:
    """Per-shot cinematography estimates from Claude vision, grounded in the measurements."""
    from pydantic import BaseModel, Field

    from .writer import _image_block

    class ShotReading(BaseModel):
        shot: int
        shot_type: str
        primary_subject: str
        camera_height_angle: str
        lens_estimate: str = Field(description="Plausible focal length range, labelled as an estimate")
        depth_of_field: str
        camera_movement: str
        subject_movement: str
        lighting_setup: str
        palette: str
        atmosphere: str
        composition: str
        transition_out: str
        why_cinematic: str

    class Reading(BaseModel):
        shots: list[ShotReading]
        style_prompt: str = Field(description="One paragraph to prepend to every generation prompt")
        shot_scale_pattern: list[str] = Field(description="One short line per shot, in order")

    content: list = []
    for s in report["shots"]:
        content.append({"type": "text", "text": f"Shot {s['shot']} ({s['start']}-{s['end']}s) measurements: "
                        + json.dumps({k: v for k, v in s.items() if k not in ('frames',)})})
        content += [_image_block(out_dir / "frames" / f) for f in s["frames"]]
    content.append({"type": "text", "text": "Describe each shot's cinematography. Ground camera movement in the "
                                            "measured motion; label lens and aperture as estimates."})
    r = claude.ask("You are a cinematographer breaking down a reference clip shot by shot.", content, Reading,
                   claude.s.claude.effort_qc)
    report["style_prompt"] = r.style_prompt
    report["shot_scale_pattern"] = r.shot_scale_pattern
    return [x.model_dump() for x in r.shots]


def to_markdown(r: dict) -> str:
    lines = [f"# Reference analysis: {r['source']}", "",
             f"{r['duration']:.2f} s, {r['width']}x{r['height']}, {r['fps']} fps, {r['video_codec']}, "
             f"{r['shot_count']} shots (average {r['avg_shot_seconds']} s, range {r['shot_seconds_range']})", "",
             f"Exposure: 5th-percentile luma {r['luma_p5']}, 95th {r['luma_p95']}; saturation {r['saturation']}; "
             f"warm pixels {r['warm_share'] * 100:.0f}%", ""]
    if r.get("audio"):
        lines += [f"Audio: {r['audio']['integrated_lufs']} LUFS integrated, LRA {r['audio']['loudness_range_lu']} LU",
                  ""]
    lines += ["![contact sheet](contact_sheet.jpg)", "", "| # | time | s | camera motion | DoF cue | subject | light |",
              "|---|---|---|---|---|---|---|"]
    for s in r["shots"]:
        lines.append(f"| {s['shot']} | {s['start']:.2f}-{s['end']:.2f} | {s['seconds']:.2f} | "
                     f"{'; '.join(s['camera_motion'])} | {s['dof_cue']} | {s['detail_centroid']} | {s['light_cue']} |")
    if r.get("cinematography"):
        lines += ["", "## Cinematography (Claude estimates)", ""]
        for c in r["cinematography"]:
            lines += [f"**Shot {c['shot']}: {c['shot_type']}** - {c['primary_subject']}. {c['camera_height_angle']}; "
                      f"{c['lens_estimate']}; {c['depth_of_field']}. Camera: {c['camera_movement']}. Subject: "
                      f"{c['subject_movement']}. Light: {c['lighting_setup']}. {c['atmosphere']}. "
                      f"Composition: {c['composition']}. Out: {c['transition_out']}. Why it works: {c['why_cinematic']}", ""]
    lines += ["", *[f"_{n}_" for n in r["notes"]]]
    return "\n".join(lines) + "\n"


def apply_to_style(report: dict, style_dir: Path) -> Path:
    """Merge measured values into style/style_bible.json (keeps hand-written fields)."""
    p = style_dir / "style_bible.json"
    bible = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    bible.update({
        "source": f"Measured from reference {report['source']} ({report['width']}x{report['height']}, "
                  f"{report['fps']} fps, {report['shot_count']} shots). Style reference only; no footage reused.",
        "fps": round(report["fps"]), "avg_shot_seconds": report["avg_shot_seconds"],
        "shot_seconds_range": report["shot_seconds_range"], "saturation": report["saturation"],
        "warm_pixel_share": report["warm_share"],
        "exposure": f"5th-percentile luma ~{report['luma_p5']:.0f}/255, 95th ~{report['luma_p95']:.0f}",
        "palette_hex": sorted({c for s in report["shots"] for c in s["palette"][:2]})[:8],
        "reference_shots": [{"seconds": s["seconds"], "camera_motion": s["camera_motion"], "dof": s["dof_cue"],
                             "subject": s["detail_centroid"], "light": s["light_cue"]} for s in report["shots"]],
    })
    if report.get("shot_scale_pattern"):
        bible["shot_scale_pattern"] = report["shot_scale_pattern"]
    if report.get("style_prompt"):
        bible["style_prompt"] = report["style_prompt"]
    if report.get("audio"):
        bible["audio"] = f"integrated loudness {report['audio']['integrated_lufs']} LUFS"
    p.write_text(json.dumps(bible, indent=2, ensure_ascii=False), encoding="utf-8")
    return p
