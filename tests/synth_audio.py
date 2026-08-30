"""Sounds with a known shape, so the ear can be judged rather than admired.

There is no corpus of labelled Kick audio to test against, and "it looked
right when I listened to one clip" is not a test. So the shapes the detector
claims to recognise are synthesised here from their definitions - a laugh as a
voiced pulse train at the syllable rate, speech as shallower and irregular
modulation with gaps, music as continuous and bass-heavy - and the detector is
asked to tell them apart. That does not prove it works on a real stream. It
does prove it is measuring the thing it says it measures, which is the half
that was previously guesswork.
"""
from __future__ import annotations

import functools
import math
import random
import struct
import tempfile
import wave
from pathlib import Path

RATE = 16000


def write(name: str, samples: list[float], out: Path | None = None) -> Path:
    OUT = out or Path(tempfile.gettempdir()) / "clipengine-synth"
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(v * 32767)))) for v in samples))
    return path


def voiced(t: float, f0: float) -> float:
    """A glottal-ish tone: a fundamental plus the harmonics that make it a voice."""
    return sum(math.sin(2 * math.pi * f0 * h * t) / h for h in (1, 2, 3, 4, 5)) / 2.3


@functools.cache
def laughter(seconds: float, *, rate_hz: float = 4.7, level: float = 0.30,
             jitter: float = 0.08, seed: int = 1) -> list[float]:
    """Voiced bursts at the syllable rate of a laugh: ha-ha-ha-ha."""
    rng = random.Random(seed)
    out = []
    period = 1.0 / rate_hz
    for i in range(int(seconds * RATE)):
        t = i / RATE
        phase = (t % period) / period
        # ~40% duty: a burst then a gap, which is what makes it a pulse train.
        burst = math.sin(math.pi * phase / 0.42) if phase < 0.42 else 0.0
        f0 = 320 * (1.0 + rng.uniform(-jitter, jitter))
        out.append(level * burst * burst * (voiced(t, f0) + rng.uniform(-0.12, 0.12)))
    return out


@functools.cache
def speech(seconds: float, *, level: float = 0.22, seed: int = 2,
           bright: float = 1.0) -> list[float]:
    """Talking: syllables around 4 Hz, but irregular and much shallower."""
    rng = random.Random(seed)
    out, gate, next_change, on = [], 0.0, 0.0, True
    for i in range(int(seconds * RATE)):
        t = i / RATE
        if t >= next_change:
            on = not on if rng.random() < 0.75 else on
            next_change = t + rng.uniform(0.10, 0.34)
        target = 1.0 if on else 0.28
        gate += (target - gate) * 0.0015          # syllables blur into each other
        f0 = 130 * (1.0 + rng.uniform(-0.05, 0.05))
        tone = voiced(t, f0) + bright * 0.35 * math.sin(2 * math.pi * 2400 * t) * gate
        out.append(level * gate * (tone + rng.uniform(-0.05, 0.05)))
    return out


@functools.cache
def music(seconds: float, *, level: float = 0.30, bpm: float = 128.0, seed: int = 3):
    """A mix: bass-heavy, continuous, and modulating hard on a steady grid."""
    rng = random.Random(seed)
    out = []
    beat = bpm / 60.0
    for i in range(int(seconds * RATE)):
        t = i / RATE
        # Sixteenths land at 8.5 Hz - squarely inside the laughter band.
        pump = 0.45 + 0.55 * abs(math.sin(math.pi * beat * 4 * t)) ** 3
        kick = math.sin(2 * math.pi * 55 * t) * (1.0 if (t * beat) % 1.0 < 0.15 else 0.25)
        pad = 0.5 * (math.sin(2 * math.pi * 220 * t) + math.sin(2 * math.pi * 330 * t))
        out.append(level * pump * (0.75 * kick + 0.35 * pad + rng.uniform(-0.02, 0.02)))
    return out


@functools.cache
def room(seconds: float, level: float = 0.004, seed: int = 4):
    rng = random.Random(seed)
    return [level * rng.uniform(-1, 1) for _ in range(int(seconds * RATE))]


def join(*parts):
    out = []
    for part in parts:
        out.extend(part)
    return out


@functools.cache
def _noise(seconds: float, seed: int, low: float, high: float):
    """Band-limited noise: turbulence, which is what breath is.

    Two one-pole filters over white noise. Not a good filter, but the question
    being asked of it is "is the energy spread out or piled up", and for that
    it is the right shape.
    """
    rng = random.Random(seed)
    out, lp, hp = [], 0.0, 0.0
    a_low = min(1.0, high / (RATE / 2))
    a_high = min(1.0, low / (RATE / 2))
    for _ in range(int(seconds * RATE)):
        white = rng.uniform(-1, 1)
        lp += (white - lp) * a_low
        hp += (lp - hp) * a_high
        out.append(lp - hp)
    return tuple(out)


@functools.cache
def gasp(seconds: float = 0.35, *, level: float = 0.45, seed: int = 21):
    """A sharp intake of breath.

    Noise, because air past a narrowed glottis has no fundamental. Very fast
    attack - a tenth of a second - and then gone, because what follows a
    breath in is holding it. Weighted high, where breath noise lives.
    """
    body = _noise(seconds, seed, 900.0, 6000.0)
    out = []
    for i, v in enumerate(body):
        t = i / RATE
        rise = min(1.0, t / 0.09) ** 0.6
        fall = max(0.0, 1.0 - max(0.0, t - 0.16) / max(seconds - 0.16, 1e-6)) ** 2
        out.append(level * v * rise * fall)
    return tuple(out)


@functools.cache
def sigh(seconds: float = 1.6, *, level: float = 0.22, seed: int = 22):
    """A long breath out: slow in, long, and decaying to nothing."""
    body = _noise(seconds, seed, 250.0, 2200.0)
    out = []
    for i, v in enumerate(body):
        t = i / RATE
        rise = min(1.0, t / 0.30)
        fall = max(0.0, 1.0 - t / seconds) ** 1.6
        voiced_part = 0.25 * math.sin(2 * math.pi * (150 - 40 * t / seconds) * t)
        out.append(level * rise * fall * (v + voiced_part))
    return tuple(out)


def louder(part, factor):
    return [v * factor for v in part]


# The generators are memoised because the tests reuse the same twenty seconds
# of talking as a bed under a dozen different events, and synthesising half a
# million samples in Python is the slow half of this file.
