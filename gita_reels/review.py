"""What a reviewer looks at before a reel publishes (master prompt Step 14).

    review/contact_sheet.jpg   one frame every 0.5 s, labelled with its time and shot number
    review/loudness.png        momentary loudness across the reel, with the beat map and the planned silence
    review/audio.json          integrated loudness, true peak, loudness per beat and the checks below

The checks are measurements, not a verdict: does the level follow the beat map's intensity, is the planned
silence actually quiet, is the master at -14 LUFS / -1 dBTP.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .writer import ReelPlan

STEP = 0.5


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def contact_sheet(final: Path, out: Path, plan: ReelPlan, step: float = STEP, cols: int = 8) -> Path:
    from PIL import Image, ImageDraw

    from .edit import font
    total = _duration(final)
    starts, t = [], 0.0
    for sh in plan.shots:
        starts.append(t)
        t += sh.on_screen_seconds
    tiles, w, h = [], 216, 384
    stamps = [round(k * step, 2) for k in range(int(total / step) + 1) if k * step < total - 0.02]
    for ts in stamps:
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{ts + 0.02:.3f}", "-i", str(final),
                              "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        im = Image.frombytes("RGB", (w, h), raw[: w * h * 3]) if len(raw) >= w * h * 3 else Image.new("RGB", (w, h))
        shot = max(i for i, s in enumerate(starts) if s <= ts + 1e-6) + 1
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, w, 26], fill=(0, 0, 0))
        d.text((6, 4), f"{ts:4.1f}s  shot {shot}", font=font(18, serif=False), fill=(255, 255, 255))
        tiles.append(im)
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + 6) + 6, rows * (h + 6) + 6), (20, 20, 20))
    for k, im in enumerate(tiles):
        sheet.paste(im, (6 + (k % cols) * (w + 6), 6 + (k // cols) * (h + 6)))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=86)
    return out


def loudness(final: Path) -> dict:
    """EBU R128: integrated loudness, true peak and momentary loudness (400 ms window) every 0.1 s."""
    log = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(final), "-vn", "-af",
                          "ebur128=peak=true:metadata=0", "-f", "null", "-"], capture_output=True, text=True).stderr
    curve = [(float(t), float(m)) for t, m in re.findall(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-inf)", log)
             if m != "-inf"]
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    peak = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", log)
    return {"integrated_lufs": float(i[-1]) if i else None,
            "true_peak_dbtp": float(peak[-1]) if peak and peak[-1] != "-inf" else None,
            "momentary": curve}


def _mean(curve: list[tuple[float, float]], a: float, b: float) -> float | None:
    # momentary loudness at t covers the 400 ms before t; floor at -70 like the meter
    xs = [max(m, -70.0) for t, m in curve if a + 0.4 <= t <= b]
    return round(sum(xs) / len(xs), 1) if xs else None


def checks(plan: ReelPlan, audio: dict) -> dict:
    curve, out = audio["momentary"], {}
    lufs, tp = audio["integrated_lufs"], audio["true_peak_dbtp"]
    out["loudness_ok"] = lufs is not None and abs(lufs + 14) <= 1.0
    out["true_peak_ok"] = tp is not None and tp <= -0.9
    cue = plan.cue_sheet
    if not cue:
        return out
    beats = [{"name": b.name, "start": b.start, "end": b.end, "intensity": b.intensity,
              "mean_lufs_m": _mean(curve, b.start, b.end)} for b in cue.beats]
    out["beats"] = beats
    pairs = [(b["intensity"], b["mean_lufs_m"]) for b in beats if b["mean_lufs_m"] is not None]
    if len(pairs) >= 3:
        import numpy as np
        x, y = np.array(pairs, dtype=float).T
        out["intensity_correlation"] = round(float(np.corrcoef(x, y)[0, 1]), 2) if x.std() and y.std() else None
        out["follows_beat_map"] = (out["intensity_correlation"] or 0) >= 0.6
    if cue.silence:
        s = cue.silence
        inside = _mean(curve, s.start, s.end)
        before = _mean(curve, max(0.0, s.start - 2.0), s.start)
        out["silence"] = {"start": s.start, "end": s.end, "mean_lufs_m": inside, "two_seconds_before": before,
                          "drop_db": round(before - inside, 1) if inside is not None and before is not None else None}
        out["silence_ok"] = bool(out["silence"]["drop_db"] and out["silence"]["drop_db"] >= 10)
    return out


def loudness_chart(plan: ReelPlan, audio: dict, out: Path) -> Path:
    from PIL import Image, ImageDraw

    from .edit import font
    curve = audio["momentary"]
    total = sum(sh.on_screen_seconds for sh in plan.shots)
    W, H, L, B = 1400, 420, 60, 40
    im = Image.new("RGB", (W, H), (250, 250, 248))
    d = ImageDraw.Draw(im)
    f = font(14, serif=False)

    def X(t):
        return L + (W - L - 20) * t / max(total, 0.1)

    def Y(db):
        return 20 + (H - 20 - B) * (-max(min(db, 0), -60)) / 60

    cue = plan.cue_sheet
    if cue:
        for k, b in enumerate(cue.beats):
            shade = 255 - int(12 * b.intensity)
            d.rectangle([X(b.start), 20, X(b.end), H - B], fill=(shade, shade, 255))
            d.text((X(b.start) + 4, 24), f"{b.name} ({b.intensity})", font=f, fill=(40, 40, 90))
            for h in b.hits:
                d.line([X(h), 20, X(h), H - B], fill=(200, 60, 40), width=2)
        if cue.silence:
            d.rectangle([X(cue.silence.start), H - B - 14, X(cue.silence.end), H - B], fill=(30, 30, 30))
    for db in (-14, -23, -40):
        d.line([L, Y(db), W - 20, Y(db)], fill=(170, 170, 170))
        d.text((6, Y(db) - 8), f"{db} LU", font=f, fill=(90, 90, 90))
    pts = [(X(t - 0.2), Y(m)) for t, m in curve]          # centre the 400 ms window
    if len(pts) > 1:
        d.line(pts, fill=(20, 20, 20), width=2)
    t = 0.0
    for k, sh in enumerate(plan.shots):
        d.line([X(t), H - B, X(t), H - B + 8], fill=(0, 0, 0))
        d.text((X(t) + 2, H - B + 8), str(k + 1), font=f, fill=(0, 0, 0))
        t += sh.on_screen_seconds
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


def review_final(final: Path, out_dir: Path, plan: ReelPlan) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    contact_sheet(final, out_dir / "contact_sheet.jpg", plan)
    audio = loudness(final)
    loudness_chart(plan, audio, out_dir / "loudness.png")
    report = {"integrated_lufs": audio["integrated_lufs"], "true_peak_dbtp": audio["true_peak_dbtp"],
              **checks(plan, audio)}
    (out_dir / "audio.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
