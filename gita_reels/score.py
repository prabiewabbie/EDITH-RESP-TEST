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
