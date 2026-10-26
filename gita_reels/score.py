"""Original background score, synthesised on the fly so every reel has music with no licence question.

A tanpura-style drone (four plucked strings, Pa-Sa-Sa-low Sa, Karplus-Strong with a jawari buzz) is the bed
for every mood; epic and triumphant add low frame-drum hits that build, tension swaps Pa for a dissonant Ma
and adds a slow heartbeat. It is a stand-in until you add tracks to music/, not a replacement for a composer.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
SA = 138.59  # C#3, a common tanpura tonic


def _pluck(freq: float, seconds: float, rng: np.random.Generator, bright: float = 0.996) -> np.ndarray:
    n, period = int(SR * seconds), int(round(SR / freq))
    buf = rng.uniform(-1, 1, period)
    out = np.empty(n)
    for start in range(0, n, period):  # Karplus-Strong, one period at a time
        chunk = buf[: min(period, n - start)]
        out[start:start + len(chunk)] = chunk
        buf = bright * 0.5 * (buf + np.roll(buf, -1))
    out = np.tanh(2.2 * out) / np.tanh(2.2)  # jawari: the bridge's buzzing overtones
    return out * np.exp(-np.arange(n) / (SR * seconds * 0.45))


def _drone(total: float, rng: np.random.Generator, first: float) -> np.ndarray:
    n = int(SR * total)
    out = np.zeros(n)
    strings = [_pluck(f, 4.0, rng) for f in (first, SA * 2, SA * 2, SA)]
    step, t = 0.9, 0.0  # one string every 0.9 s, the slow tanpura cycle
    k = 0
    while t < total:
        s = strings[k % 4] * (0.8 + 0.2 * rng.random())
        i = int(t * SR)
        m = min(len(s), n - i)
        out[i:i + m] += s[:m]
        t += step * (1.6 if k % 4 == 3 else 1.0)  # the pause after the low Sa
        k += 1
    return out


def _drum(seconds: float, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    body = np.sin(2 * np.pi * (70 * t - 18 * t * t)) * np.exp(-t * 6)
    skin = rng.uniform(-1, 1, len(t)) * np.exp(-t * 40) * 0.25
    return body + skin


def compose(mood: str, total: float, seed: int = 0) -> np.ndarray:
    """Return a stereo float array (n, 2) in -1..1."""
    rng = np.random.default_rng(seed)
    first = SA * 4 / 3 if mood == "tension" else SA * 3 / 2  # Ma instead of Pa
    left, right = _drone(total, rng, first), _drone(total, rng, first)
    n = len(left)
    t = np.arange(n) / SR
    bed = np.stack([left, right], axis=1) * 0.35
    bed += 0.06 * np.sin(2 * np.pi * SA / 2 * t)[:, None]  # low Sa sustained under the plucks
    if mood in ("epic", "triumphant"):
        hit = _drum(1.2, rng)
        beat = 1.25 if mood == "epic" else 0.95
        x = 0.0
        while x < total - 0.3:
            i = int(x * SR)
            m = min(len(hit), n - i)
            gain = 0.25 + 0.55 * min(x / max(total * 0.7, 1), 1)  # builds toward the reveal
            bed[i:i + m] += (hit[:m] * gain)[:, None]
            x += beat
    elif mood == "tension":
        hit = _drum(0.5, rng) * 0.5
        x = 0.4
        while x < total - 0.3:
            for off in (0.0, 0.28):  # lub-dub
                i = int((x + off) * SR)
                m = min(len(hit), n - i)
                if m > 0:
                    bed[i:i + m] += (hit[:m] * (1.0 if off == 0 else 0.6))[:, None]
            x += 1.6
    swell = 0.75 + 0.25 * np.sin(2 * np.pi * t / max(total, 1) - np.pi / 2)
    bed *= swell[:, None]
    return bed / max(np.abs(bed).max(), 1e-6) * 0.8


def write(mood: str, total: float, path: Path, seed: int = 0) -> Path:
    pcm = (compose(mood, total, seed) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


# --- scored reels: the cue sheet drives the music ------------------------------------------------
# A plan with a cue_sheet gets a bed written to its sections (fal.music_model, or the drone above when none is
# configured), then shaped here so the picture's events land exactly: the level follows each beat's intensity,
# everything drops out for the planned silence except one element, the bed ducks under every hit, and designed
# impacts and risers sit on the exact frames the cue sheet names. Generated music cannot be trusted to put a
# hit on a frame; this layer can.

def _level(intensity: int) -> str:
    return ("very soft, sparse" if intensity <= 3 else "moderate, building" if intensity <= 6
            else "loud, driving" if intensity <= 8 else "full force, climactic")


def sections(cue, total: float, min_ms: int = 3000) -> list[dict]:
    """Composition-plan sections from the beats; beats under 3 s (the model's minimum) merge into a neighbour.
    Durations add up to the reel length exactly."""
    beats = sorted(cue.beats, key=lambda b: b.start)
    groups: list[list] = []
    for b in beats:
        if groups and (groups[-1][-1].end - groups[-1][0].start) * 1000 < min_ms:
            groups[-1].append(b)
        else:
            groups.append([b])
    if len(groups) > 1 and (groups[-1][-1].end - groups[-1][0].start) * 1000 < min_ms:
        groups[-2] += groups.pop()
    out, used = [], 0
    for k, g in enumerate(groups):
        end = total if k == len(groups) - 1 else g[-1].end
        ms = max(min_ms, int(round(end * 1000)) - used)
        used += ms
        peak = max(b.intensity for b in g)
        styles = [f"{b.instruments}" + (f", {b.dynamics}" if b.dynamics else "") for b in g]
        out.append({"section_name": " / ".join(b.name for b in g)[:100],
                    "positive_local_styles": styles + [_level(peak)],
                    "negative_local_styles": ["vocals with lyrics", "spoken word"],
                    "duration_ms": ms, "lines": []})
    return out


def bed_request(cue, total: float) -> dict:
    """Request body for fal-ai/elevenlabs/music (composition_plan with strict section durations)."""
    return {"composition_plan": {
        "positive_global_styles": [f"film score", f"{cue.tempo:.0f} BPM", f"key of {cue.key}", *cue.styles],
        "negative_global_styles": ["lyrics", "pop drums", "EDM", "devotional bhajan", *cue.avoid],
        "sections": sections(cue, total)},
        "respect_sections_durations": True, "output_format": "mp3_44100_128"}


def _read(path: Path, total: float) -> np.ndarray:
    import subprocess
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR),
                          "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, "<f4").reshape(-1, 2).astype(np.float64)
    n = int(SR * total)
    return np.pad(x, ((0, max(0, n - len(x))), (0, 0)))[:n]


def _impact(rng: np.random.Generator) -> np.ndarray:
    t = np.arange(int(SR * 1.6)) / SR
    body = np.sin(2 * np.pi * (95 * t - 22 * t * t)) * np.exp(-t * 3.2)        # taiko: pitch falls as it rings
    sub = np.sin(2 * np.pi * 42 * t) * np.exp(-t * 2.2) * 0.8
    crack = rng.uniform(-1, 1, len(t)) * np.exp(-t * 55) * 0.6                   # stick and skin transient
    return np.tanh(1.6 * (body + sub + crack))


def _riser(seconds: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * seconds)
    t = np.arange(n) / SR
    env = (t / seconds) ** 2.2
    noise = np.convolve(rng.uniform(-1, 1, n), np.ones(6) / 6, mode="same")      # soft high noise
    sweep = np.sin(2 * np.pi * np.cumsum(180 + 900 * (t / seconds) ** 2) / SR)
    return (0.55 * noise + 0.45 * sweep) * env


def _keep(kind: str, seconds: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SR * seconds)
    out = np.zeros(n)
    if kind == "heartbeat":
        hit = _drum(0.45, rng)
        for x in np.arange(0.05, seconds, 0.85):
            for off, g in ((0.0, 1.0), (0.26, 0.6)):
                i = int((x + off) * SR)
                m = min(len(hit), n - i)
                if m > 0:
                    out[i:i + m] += hit[:m] * g * 0.7
    elif kind in ("breath", "wind"):
        k = 40 if kind == "breath" else 160
        noise = np.convolve(rng.uniform(-1, 1, n + k), np.ones(k) / k, mode="valid")[:n] * (8 if kind == "breath" else 14)
        t = np.arange(n) / SR
        env = (np.sin(np.pi * np.clip(t / min(seconds, 1.4), 0, 1)) if kind == "breath"
               else 0.6 + 0.4 * np.sin(2 * np.pi * t / 2.3))
        out = noise * env * 0.5
    return out


def envelope(cue, total: float, ramp: float = 0.12) -> np.ndarray:
    """Gain per sample for the bed: beat intensity (10 = 0 dB, 1 = -16 dB), -5 dB ducks under hits, and a hard
    drop to nothing for the planned silence."""
    n = int(SR * total)
    t = np.arange(n) / SR
    db = np.full(n, -12.0)
    for b in cue.beats:
        db[(t >= b.start) & (t < b.end)] = -(10 - b.intensity) * 16 / 9
    k = max(1, int(ramp * SR))
    db = np.convolve(np.pad(db, (k // 2, k - k // 2 - 1), mode="edge"), np.ones(k) / k, mode="valid")
    for b in cue.beats:
        for h in b.hits:
            m = (t >= h) & (t < h + 0.35)
            db[m] -= 5 * np.exp(-(t[m] - h) / 0.35)
    g = 10 ** (db / 20)
    if cue.silence:
        s = cue.silence
        fade_out, fade_in = 0.03, 0.02
        g *= np.clip(np.maximum((s.start - t) / fade_out, (t - s.end) / fade_in), 0, 1)
    return g


def scored(cue, total: float, path: Path, bed: Path | None = None, mood: str = "epic", seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    x = _read(bed, total) if bed else compose(mood, total, seed)
    x = x / max(np.abs(x).max(), 1e-6) * 0.7
    x *= envelope(cue, total)[:, None]
    n = len(x)
    hits = sorted({h for b in cue.beats for h in b.hits})
    for r in cue.risers:
        seconds = min(1.6, r)
        z = _riser(seconds, rng) * 0.35
        i = int((r - seconds) * SR)                     # ends exactly on its hit
        m = min(len(z), n - i)
        if m > 0:
            x[i:i + m] += z[:m, None]
    for h in hits:
        z = _impact(rng)
        i = int(h * SR)
        m = min(len(z), n - i)
        if m > 0:
            x[i:i + m] += z[:m, None] * 0.9
    if cue.silence and cue.silence.keep != "none":
        s = cue.silence
        z = _keep(s.keep, s.end - s.start, rng) * 0.5
        i = int(s.start * SR)
        m = min(len(z), n - i)
        x[i:i + m] += z[:m, None]
    x = x / max(np.abs(x).max(), 1e-6) * 0.89
    pcm = (x * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path
