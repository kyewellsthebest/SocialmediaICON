"""Procedural ambience: the sound that sits under everything.

Radio carrier, four-engine rumble, cockpit noise, tape hiss with wow and
flutter, and the UVB-76 buzz. Synthesised rather than sampled because these are
textures, not recordings - and because a synthesised bed carries no licence
question at all.

The buzzer is the honest one: the real transmitter emits a synthetic tone, so a
synthesised version of it is not a stand-in for the sound, it is the sound.

Pure standard library. Nothing here needs numpy, and adding a numerical stack
to render forty seconds of noise would be a poor trade.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 22050
PEAK = 32767


def _white(seed: int) -> "callable[[], float]":  # noqa: UP037 - runtime-only hint
    """A small deterministic noise source.

    A linear congruential generator rather than `random`, so a bed rendered
    today matches one rendered next month and "regenerate and compare" keeps
    meaning something.
    """
    state = seed & 0xFFFFFFFF

    def step() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return (state / 0x7FFFFFFF) - 1.0

    return step


class OnePole:
    """One-pole low-pass. Chained twice it is enough of a filter for a bed."""

    def __init__(self, cutoff_hz: float, rate: int = RATE) -> None:
        self.a = math.exp(-2.0 * math.pi * cutoff_hz / rate)
        self.z = 0.0

    def low(self, x: float) -> float:
        self.z = self.a * self.z + (1.0 - self.a) * x
        return self.z

    def high(self, x: float) -> float:
        return x - self.low(x)


#: name -> (noise gain, low cutoff, high cutoff, hum hz, hum gain)
RECIPES: dict[str, tuple[float, float, float, float, float]] = {
    "radio": (0.16, 2600.0, 600.0, 52.0, 0.05),
    "prop": (0.20, 900.0, 60.0, 88.0, 0.09),
    "cockpit": (0.20, 1200.0, 70.0, 64.0, 0.07),
    "room": (0.05, 700.0, 40.0, 58.0, 0.03),
    "tape": (0.13, 8000.0, 2600.0, 46.0, 0.04),
    "buzz": (0.07, 3000.0, 500.0, 0.0, 0.0),
}

#: The Buzzer runs at roughly 25 tones a minute: a beat on, a beat off.
BUZZ_PERIOD_S = 2.4
BUZZ_ON_S = 1.12
BUZZ_HZ = 760.0


def _buzz(t: float) -> float:
    """One cycle of the UVB-76 tone, with the edges rounded off."""
    phase = t % BUZZ_PERIOD_S
    if phase > BUZZ_ON_S:
        return 0.0
    edge = 0.02
    envelope = min(1.0, phase / edge, (BUZZ_ON_S - phase) / edge)
    # A square would alias badly at this sample rate; two harmonics of a saw
    # give the same harsh character without the fizz.
    wave_ = (
        math.sin(2 * math.pi * BUZZ_HZ * t)
        + 0.45 * math.sin(4 * math.pi * BUZZ_HZ * t)
        + 0.25 * math.sin(math.pi * BUZZ_HZ * t)
    )
    return envelope * wave_ * 0.30


def synth(kind: str, seconds: float, dest: Path | str, *, gain: float = 1.0, seed: int = 7) -> Path:
    """Write `seconds` of the named bed to a 16-bit mono wav."""
    noise_gain, low_hz, high_hz, hum_hz, hum_gain = RECIPES.get(kind, RECIPES["room"])
    rng = _white(seed)
    low = OnePole(low_hz)
    high = OnePole(high_hz)
    # A slow wander on the tape filter is what "wow and flutter" actually is.
    flutter = kind == "tape"

    count = max(1, int(seconds * RATE))
    frames = bytearray()
    for i in range(count):
        t = i / RATE
        sample = high.high(low.low(rng())) * noise_gain
        if hum_gain:
            sample += math.sin(2 * math.pi * hum_hz * t) * hum_gain
        if flutter:
            sample *= 1.0 + 0.18 * math.sin(2 * math.pi * 0.55 * t)
        if kind == "buzz":
            sample += _buzz(t)
        # A short fade at each end so the bed does not click in or out.
        if t < 0.4:
            sample *= t / 0.4
        remaining = seconds - t
        if remaining < 0.6:
            sample *= max(0.0, remaining / 0.6)
        value = int(max(-1.0, min(1.0, sample * gain)) * PEAK)
        frames += struct.pack("<h", value)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))
    return dest
