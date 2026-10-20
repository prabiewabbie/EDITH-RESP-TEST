"""Programmatic checks on an exported file: format, streams, black or frozen frames, clipping, loudness.

Returns {"ok": bool, "checks": [{"name", "ok", "detail"}], "summary": {...}}. "ok" is False if any hard check
fails; soft findings (loudness off target) are reported with ok=True and a warning.
"""
from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _filter_log(path: Path, vf: str | None = None, af: str | None = None) -> str:
    args = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path)]
    if vf:
        args += ["-vf", vf, "-an"]
    if af:
        args += ["-af", af, "-vn"]
    args += ["-f", "null", "-"]
    return subprocess.run(args, capture_output=True, text=True).stderr


def validate_export(path: Path, width: int, height: int, fps: int, seconds: float, expect_audio: bool = True,
                    tolerance: float = 0.15, freeze_seconds: float = 1.5) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, warn: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, **({"warning": True} if warn else {})})

    if not path.exists():
        return {"ok": False, "checks": [{"name": "file", "ok": False, "detail": f"{path} does not exist"}],
                "summary": {}}
    info = _probe(path)
    video = [st for st in info["streams"] if st["codec_type"] == "video"]
    audio = [st for st in info["streams"] if st["codec_type"] == "audio"]
    add("video stream", len(video) == 1, f"{len(video)} video stream(s)")
    if not video:
        return {"ok": False, "checks": checks, "summary": {}}
    v = video[0]
    add("dimensions", (v["width"], v["height"]) == (width, height), f"{v['width']}x{v['height']} "
        f"(expected {width}x{height})")
    sar = v.get("sample_aspect_ratio", "1:1")
    add("square pixels", sar in ("1:1", "0:1"), f"sample aspect ratio {sar}")
    rate = float(Fraction(v.get("avg_frame_rate", "0/1"))) if v.get("avg_frame_rate", "0/0") != "0/0" else 0.0
    add("frame rate", abs(rate - fps) < 0.01, f"{rate:.3f} fps (expected {fps})")
    add("pixel format", v.get("pix_fmt") == "yuv420p", f"{v.get('pix_fmt')} (expected yuv420p)")
    dur = float(info["format"].get("duration", 0))
    add("duration", abs(dur - seconds) <= tolerance, f"{dur:.2f}s (expected {seconds:.2f}s)")
    add("codec", v["codec_name"] in ("h264", "hevc"), v["codec_name"])
    if expect_audio:
        add("audio stream", len(audio) == 1 and audio[0]["codec_name"] == "aac",
            f"{len(audio)} audio stream(s) {[a['codec_name'] for a in audio]}")

    black = re.findall(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)",
                       _filter_log(path, vf="blackdetect=d=0.1:pic_th=0.97:pix_th=0.06"))
    add("black frames", not black, "none" if not black else
        "; ".join(f"{float(a):.2f}-{float(b):.2f}s" for a, b, _ in black))
    frozen = re.findall(r"freeze_start: ([\d.]+)[\s\S]*?freeze_duration: ([\d.]+)",
                        _filter_log(path, vf=f"freezedetect=n=-60dB:d={freeze_seconds}"))
    add("frozen picture", not frozen, "none" if not frozen else
        "; ".join(f"from {float(a):.2f}s for {float(d):.2f}s" for a, d in frozen))

    summary = {"seconds": round(dur, 3), "width": v["width"], "height": v["height"], "fps": round(rate, 3),
               "video_codec": v["codec_name"], "pix_fmt": v.get("pix_fmt"),
               "size_mb": round(int(info["format"].get("size", 0)) / 1e6, 2)}
    if audio:
        log = _filter_log(path, af="volumedetect,ebur128=peak=true")
        peak = re.search(r"max_volume: (-?[\d.]+) dB", log)
        lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
        tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", log)
        peak_db = float(peak.group(1)) if peak else None
        add("audio clipping", peak_db is not None and peak_db < -0.1, f"sample peak {peak_db} dBFS")
        if lufs:
            loud = float(lufs[-1])
            add("loudness", True, f"{loud:.1f} LUFS integrated (target -14)", warn=abs(loud + 14) > 2)
            summary["lufs"] = loud
        if tp:
            summary["true_peak_dbfs"] = float(tp[-1])
        summary["audio_codec"] = audio[0]["codec_name"]
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks, "summary": summary}
