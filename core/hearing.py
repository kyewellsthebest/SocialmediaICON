"""What the sound is *doing* - laughter, shouting, speech, music, a room going quiet.

Loudness on its own cannot tell a moment from a mixing desk. Music playing over
a stream is loud for twenty minutes; a crowd laughing is loud for two seconds
and is the only one of the two worth clipping. The old signal - "which second
was loudest" - cannot separate them, and it was never going to.

So this reads structure instead of level, and the structure that matters is in
the **amplitude envelope**, not the spectrum:

* **Laughter** is a pulse train. Every description of it in the literature
  agrees on the shape - a series of short voiced bursts, "ha-ha-ha", repeating
  at roughly four to seven a second, each around 75ms. That repetition is a
  frequency, and it is a frequency you can measure directly: take the loudness
  curve, remove its mean, and look at how much energy sits in the 3.5-8 Hz band
  of the *envelope*. Deep, regular modulation in that band is what laughing
  sounds like from the outside.

* **Speech** modulates in the same band - the syllable rate is about 4 Hz -
  which is why modulation alone is not enough. It modulates more shallowly and
  far less regularly, so the discriminator is modulation *depth*, not presence.

* **Music** is the trap. A track at 120 BPM puts beats at 2 Hz and sixteenths
  at 8 Hz, and a compressed mix modulates deeply. What music does not do is
  start and stop: its modulation is a steady state that holds for minutes.
  Laughter is a transient. So every measure here is taken against the stream's
  own recent past, and a beat that has been there for the last forty-five
  seconds contributes nothing.

* **Shouting** is not simply louder. Raising the voice shifts energy upward -
  more effort, more high harmonics - so a shout is a level rise *and* a
  brightness rise together. Music getting louder is a level rise on its own,
  and the pair is what tells them apart.

Four band-limited copies of the audio come back from one ffmpeg pass, which is
where the spectral half comes from: how the energy is split between bass,
voice and presence says whether a sound is a person or a subwoofer.

Nothing here is a model. It is arithmetic over an envelope, and it runs in
well under a second on half a minute of audio.
"""

from __future__ import annotations

import logging
import math
import subprocess
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ffmpeg_ops import require_binaries

log = logging.getLogger(__name__)

#: Envelope resolution. 10ms is finer than the ~75ms bursts inside a laugh, so
#: the pulse train is sampled well above the rate that has to be measured.
FRAME_MS = 10
FRAME_HZ = 1000 // FRAME_MS
SAMPLE_RATE = 16000

#: Eight bands, log-spaced, because two different questions are being asked of
#: them. The coarse one is where the energy is: rumble and bass are where music
#: and traffic sit, the voice band carries nearly all speech, presence is where
#: vocal effort goes and is what makes a shout sound like a shout.
#:
#: The finer one is *what kind of sound this is*, and it cannot be answered
#: with four. A voice is harmonic - energy piled into a few places - and a
#: breath is noise, spread evenly across all of them. That difference is the
#: whole of telling a gasp from a shout, and measuring it needs enough bands
#: for "evenly" to mean something.
BANDS: tuple[tuple[str, int, int], ...] = (
    ("sub", 60, 160),
    ("bass", 160, 320),
    ("low", 320, 640),
    ("mid", 640, 1250),
    ("upper", 1250, 2500),
    ("presence", 2500, 4000),
    ("high", 4000, 6000),
    ("air", 6000, 7800),
)

#: The coarse grouping the older signals were written against, so shouting and
#: dormancy keep asking the question they were tuned for.
GROUPS: dict[str, tuple[str, ...]] = {
    "bass": ("sub", "bass"),
    "voice": ("low", "mid"),
    "upper": ("upper", "presence"),
    "presence": ("high", "air"),
}

#: Laughter's repetition rate. Provine's counts and every acoustic study since
#: put the syllable rate of a laugh in this band; 4.7 Hz is the usual centre.
LAUGH_LOW_HZ, LAUGH_HIGH_HZ = 3.5, 8.0
#: Speech syllables sit here too, so this is measured but never trusted alone.
#: These three are what separate a laugh from a sentence, and they were set by
#: running both past the detector rather than by taste: ordinary talking peaks
#: around depth 0.35 with peakiness under 3, a laugh runs past 0.5 and 4.
MIN_DEPTH = 0.34
MIN_PEAKINESS = 3.4
MIN_LIFT = 0.14
#: How long the pulsing has to hold. Nobody laughs for a quarter of a second,
#: and a splice or a cough is over in one window.
MIN_WINDOWS = 3
#: History needed before any of this means anything.
WARMUP_S = 4.0
#: How much of the energy has to move up the spectrum for a level rise to be a
#: raised voice rather than a fader. An absolute shift in balance rather than a
#: ratio, because how big a ratio a shout produces depends entirely on how
#: bright the voice already was - a ratio test passes on a dark voice and fails
#: on a bright one for the same shout.
#:
#: Swept rather than chosen: at 0.03 ordinary talking produced five shouts in
#: thirty seconds and at 0.10 a real shout was missed. 0.08 is the only value
#: that catches the shout, ignores the talking and ignores a fader being pushed.
BRIGHTEN = 0.08
ANALYSIS_S = 1.5
HOP_S = 0.25
#: How far back "usual for this stream" reaches. Long enough that a steady beat
#: is inside it, short enough to follow a change of scene.
BASELINE_S = 45.0


class HearingError(RuntimeError):
    pass


def _rms_db(chunk: bytes) -> float:
    """RMS of a 16-bit frame, in dBFS. -90 is silence."""
    try:
        import audioop
    except ImportError:  # pragma: no cover - Python 3.13 removed it
        return _rms_db_slow(chunk)
    value = audioop.rms(chunk, 2)
    return -90.0 if value <= 0 else max(-90.0, 20.0 * math.log10(value / 32768.0))


def _rms_db_slow(chunk: bytes) -> float:
    """The same sum, without the C. Kept because audioop is on its way out."""
    samples = array("h")
    samples.frombytes(chunk)
    if not samples:
        return -90.0
    mean = sum(s * s for s in samples) / len(samples)
    return -90.0 if mean <= 0 else max(-90.0, 10.0 * math.log10(mean / (32768.0**2)))


def _split_bands(src: Path | str, *, seconds: float | None = None) -> list[array]:
    """Four band-limited copies of the audio, from one decode.

    Merged into the channels of a single stream rather than fetched one at a
    time: four ffmpeg processes over the same file is four decodes of the same
    file, and the filtering itself is the cheap part.
    """
    require_binaries()
    # One more copy than there are bands: the last channel is the signal
    # untouched, because the pitch tracker needs the waveform and not a
    # filtered version of it.
    outs = len(BANDS) + 1
    taps = "".join(f"[s{i}]" for i in range(outs))
    # Downmixed to mono before anything else, and this is not a nicety.
    #
    # The nine taps are merged back into one stream of nine channels, which is
    # what the reader below unpacks. That arithmetic only holds if each tap is
    # mono: on a stereo source the merge produces eighteen channels, the
    # requested nine cannot be reconciled with them, and ffmpeg fails the
    # whole filter graph with "Error reinitializing filters". Not partially -
    # it writes no audio at all, and listen() raises.
    #
    # Every fixture in the tests is mono and every real stream is stereo, so
    # the ear passed every test and had never once worked in production. What
    # that cost: laughter and voice scored zero on every window of every
    # stream, so the only families that could ever fire were motion and faces,
    # so almost nothing ever had two families agreeing, so almost everything
    # faced the lone-signal bar and failed it. Measured on 27 minutes of real
    # video: 168 windows, 0 with two families agreeing, 141 rejected as "one
    # signal only".
    chain = [f"[0:a]aformat=channel_layouts=mono,asplit={outs}{taps}"]
    for i, (_, low, high) in enumerate(BANDS):
        chain.append(f"[s{i}]highpass=f={low},lowpass=f={high}[b{i}]")
    chain.append(f"[s{len(BANDS)}]anull[b{len(BANDS)}]")
    chain.append("".join(f"[b{i}]" for i in range(outs)) + f"amerge=inputs={outs}[out]")

    command = ["ffmpeg", "-v", "error"]
    if seconds:
        command += ["-t", f"{seconds:.2f}"]
    command += [
        "-i", str(src),
        "-filter_complex", ";".join(chain), "-map", "[out]",
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE), "-ac", str(len(BANDS) + 1), "-",
    ]
    proc = subprocess.run(command, capture_output=True)
    if not proc.stdout:
        raise HearingError(
            f"no audio from {Path(str(src)).name}: "
            f"{proc.stderr.decode('utf-8', 'replace')[-300:]}"
        )

    stride = 2 * (len(BANDS) + 1)
    interleaved = array("h")
    interleaved.frombytes(proc.stdout[: len(proc.stdout) - len(proc.stdout) % stride])
    # Extended slices on array are a C-level copy, so this is nine passes over
    # the buffer rather than a Python loop over every sample.
    channels = len(BANDS) + 1
    return [interleaved[i::channels] for i in range(channels)]


# --- the envelope, and what its shape means ---------------------------------


@dataclass
class Hearing:
    """Everything the ear can tell, second by second, over one stretch of audio."""

    window_s: float
    #: Broadband loudness per frame, dBFS.
    level_db: list[float] = field(default_factory=list)
    #: Share of energy in each band, per frame. Sums to 1.
    shares: dict[str, list[float]] = field(default_factory=dict)
    #: (start_s, end_s, confidence) where the envelope is pulsing like laughter.
    laughs: list[tuple[float, float, float]] = field(default_factory=list)
    #: (time_s, rise_db) where a voice was raised - level *and* brightness.
    shouts: list[tuple[float, float]] = field(default_factory=list)
    #: (start_s, end_s) where a loud room went abruptly quiet.
    drops: list[tuple[float, float]] = field(default_factory=list)
    #: (time_s, confidence) of a sharp intake of breath - shock, a near miss.
    gasps: list[tuple[float, float]] = field(default_factory=list)
    #: (start_s, end_s) of a long breath out - exasperation, relief, defeat.
    sighs: list[tuple[float, float]] = field(default_factory=list)
    #: How noise-like each frame is, 0..1. Breath is high, a voice is low.
    flatness: list[float] = field(default_factory=list)
    #: ...and the other way round: how periodic it is. A voice repeats at the
    #: pitch; turbulence repeats at nothing.
    voicing: list[float] = field(default_factory=list)
    #: How much of the time this sounds like a person rather than a mix.
    speech_share: float = 0.0
    music_share: float = 0.0

    @property
    def duration_s(self) -> float:
        return len(self.level_db) * self.window_s

    def time_of(self, index: int) -> float:
        return index * self.window_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_s, 2),
            "laughs": [
                {"start_s": round(a, 2), "end_s": round(b, 2), "confidence": round(c, 3)}
                for a, b, c in self.laughs
            ],
            "shouts": [{"at_s": round(t, 2), "rise_db": round(d, 1)} for t, d in self.shouts],
            "drops": [{"start_s": round(a, 2), "end_s": round(b, 2)} for a, b in self.drops],
            "gasps": [{"at_s": round(t, 2), "confidence": round(c, 2)} for t, c in self.gasps],
            "sighs": [{"start_s": round(a, 2), "end_s": round(b, 2)} for a, b in self.sighs],
            "speech_share": round(self.speech_share, 3),
            "voiced_share": (
                round(sum(1 for v in self.voicing if v > 1.0 - BREATHY) / len(self.voicing), 3)
                if self.voicing else None
            ),
            "music_share": round(self.music_share, 3),
            "mean_db": round(sum(self.level_db) / len(self.level_db), 1) if self.level_db else None,
        }


def _dft_magnitude(values: list[float], hz: float, rate: float) -> float:
    """One frequency bin, by hand.

    A whole FFT would answer a question nobody asked: the envelope is a hundred
    samples a second and only a dozen frequencies matter, so a direct sum over
    those is both shorter to read and faster to run than pulling in a library.
    """
    n = len(values)
    if n == 0:
        return 0.0
    step = 2.0 * math.pi * hz / rate
    real = sum(v * math.cos(step * i) for i, v in enumerate(values))
    imag = sum(v * math.sin(step * i) for i, v in enumerate(values))
    return 2.0 * math.hypot(real, imag) / n


@dataclass
class Pulse:
    """How, and how regularly, a stretch of sound is pulsing."""

    hz: float = 0.0
    #: Modulation amplitude over the window's mean level. A steady tone is 0
    #: however loud it is, so a quiet laugh and a loud one score alike.
    depth: float = 0.0
    #: Peak over the average across the whole modulation band. A laugh is a
    #: pulse train and puts its energy at one rate; talking wanders across
    #: every rate at once and smears. This is the difference, and without it
    #: ordinary speech reads as laughing about a third of the time.
    peakiness: float = 0.0
    #: Second half over first half. A window where the sound is arriving or
    #: leaving is a transition, not a rhythm - the envelope collapsing into
    #: silence has a beautiful pulse in it and means nothing.
    balance: float = 1.0


def _modulation(window: list[float], rate: float) -> Pulse:
    """The strongest pulsing in the laughter band, and how much to believe it."""
    mean = sum(window) / len(window) if window else 0.0
    if mean <= 1e-9:
        return Pulse()
    centred = [v - mean for v in window]

    # The whole modulation range, not only the laughter band: the average
    # across all of it is what the peak has to stand out from.
    found: list[tuple[float, float]] = []
    hz = 1.5
    while hz <= 12.0 + 1e-9:
        found.append((hz, _dft_magnitude(centred, hz, rate)))
        hz += 0.25

    inside = [(hz, mag) for hz, mag in found if LAUGH_LOW_HZ <= hz <= LAUGH_HIGH_HZ]
    best_hz, best = max(inside, key=lambda kv: kv[1]) if inside else (0.0, 0.0)
    average = sum(mag for _, mag in found) / len(found)

    half = len(window) // 2
    first = sum(window[:half]) / max(half, 1)
    second = sum(window[half:]) / max(len(window) - half, 1)

    return Pulse(
        hz=best_hz,
        depth=min(1.0, best / mean),
        peakiness=(best / average) if average > 1e-12 else 0.0,
        balance=(second / first) if first > 1e-9 else 0.0,
    )


def _flatness(energies: list[float]) -> float:
    """How evenly the energy is spread across the spectrum, 0..1.

    This is the one number that separates a breath from a voice, and it is why
    there are eight bands instead of four. A voice is *harmonic*: the vocal
    folds put energy into a fundamental and its multiples, so a few bands hold
    almost all of it and the rest hold nearly none. A breath is *turbulence* -
    air rushing past a constriction - and turbulence has no preferred
    frequency, so it lands everywhere at once.

    Geometric mean over arithmetic mean, which is the standard measure and
    exactly the right shape for the question: the geometric mean collapses the
    moment any band is empty, so it approaches the arithmetic mean only when
    every band is carrying something.
    """
    if not energies or sum(energies) <= 0:
        return 0.0
    # A band with nothing above its floor is the strongest possible evidence
    # of a peak somewhere else, so it collapses the answer rather than being
    # nudged out of the way with an epsilon.
    if any(e <= 0 for e in energies):
        return 0.0
    log_mean = sum(math.log(e) for e in energies) / len(energies)
    return min(1.0, math.exp(log_mean) / (sum(energies) / len(energies)))


#: The pitch range a human voice lives in. Below 70 Hz is a room and above 400
#: is a whistle; both are outside what a person's vocal folds do.
PITCH_LOW_HZ, PITCH_HIGH_HZ = 70.0, 400.0
#: Long enough to hold three cycles of the lowest pitch being looked for.
VOICING_FRAME_MS = 32


def voicing(signal, frames: int) -> list[float]:  # noqa: ANN001 - array("h")
    """How periodic each frame is, 0..1. A voice is; a breath is not.

    Voiced sound is the vocal folds opening and closing at the pitch, so the
    waveform repeats every three to twelve milliseconds. Turbulence - a
    breath, a fricative, a hiss - repeats at nothing. The height of the
    autocorrelation peak over that lag range is how every pitch tracker ever
    written asks the question.

    Verified against signals whose nature is not in doubt rather than against
    an opinion: white noise reads 0.10, pink noise 0.17, band-limited noise in
    the range breath occupies 0.12, a pure tone 0.84 and a stack of harmonics
    0.78. That is the separation the whole measurement rests on.

    Normalised by the frame's own zero-lag energy. Normalising by the two
    shifted halves instead - which looks more symmetrical and is what I wrote
    first - makes any smooth signal correlate with itself at every lag and
    reports 0.98 for white noise.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a dependency
        return [0.0] * frames

    x = np.frombuffer(signal.tobytes(), dtype="<i2").astype(np.float32) / 32768.0
    per_frame = SAMPLE_RATE // FRAME_HZ
    width = int(SAMPLE_RATE * VOICING_FRAME_MS / 1000)
    if len(x) < width + per_frame:
        return [0.0] * frames

    starts = np.arange(frames) * per_frame
    starts = np.clip(starts, 0, len(x) - width)
    windows = x[starts[:, None] + np.arange(width)[None, :]]
    windows = windows - windows.mean(axis=1, keepdims=True)
    energy = (windows**2).sum(axis=1) + 1e-12

    low = int(SAMPLE_RATE / PITCH_HIGH_HZ)
    high = min(int(SAMPLE_RATE / PITCH_LOW_HZ), width - 1)
    best = np.zeros(len(windows), dtype=np.float32)
    for lag in range(low, high):
        r = (windows[:, : width - lag] * windows[:, lag:]).sum(axis=1)
        np.maximum(best, r / energy, out=best)
    return [float(v) for v in np.clip(best, 0.0, 1.0)]


def _noisiness(
    raw: dict[str, list[float]], names: list[str], frames: int, *, look_back_s: float = 20.0
) -> list[float]:
    """Flatness of what rose *above* the noise floor, per frame.

    Flatness of the raw band energies does not work, and the reason is worth
    keeping: every real recording has a floor - room tone, an air conditioner,
    encoder noise - and that floor is in every band at once. So every band
    always has something in it, the geometric mean never collapses, and
    speech, breath and laughter all measure about 0.85. Measured that way the
    number says "this is a recording", which nobody asked.

    Against each band's own quiet level it says what was meant. A vowel lifts
    two bands enormously and leaves the other six at the floor, which is
    peaky. A breath lifts all eight together, which is flat. That is the
    difference between a voice and turbulence, and it is only visible once the
    floor is taken out from underneath.
    """
    window = max(1, int(look_back_s * FRAME_HZ))
    floors = {}
    for name in names:
        values = raw[name]
        floors[name] = [
            sorted(values[max(0, i - window) : i + 1])[
                max(0, (min(i + 1, window)) // 10)
            ]
            for i in range(frames)
        ]
    return [
        _flatness([max(0.0, raw[n][i] - floors[n][i]) for n in names])
        for i in range(frames)
    ]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _amplitude(db: float) -> float:
    return 10.0 ** (db / 20.0)


def listen(src: Path | str, *, seconds: float | None = None) -> Hearing:
    """Read a stretch of audio for what is happening in it."""
    channels = _split_bands(src, seconds=seconds)
    bands, whole = channels[: len(BANDS)], channels[len(BANDS)]
    per_frame = SAMPLE_RATE // FRAME_HZ
    frames = min(len(b) for b in bands) // per_frame
    if frames < int(ANALYSIS_S * FRAME_HZ):
        raise HearingError(f"only {frames} frames of audio - too short to read")

    names = [name for name, _, _ in BANDS]
    raw = {name: [0.0] * frames for name in names}
    level_db: list[float] = []

    for i in range(frames):
        low, high = i * per_frame, (i + 1) * per_frame
        energies = []
        for name, band in zip(names, bands, strict=True):
            db = _rms_db(band[low:high].tobytes())
            raw[name][i] = _amplitude(db)
            energies.append(raw[name][i])
        total = sum(energies)
        level_db.append(-90.0 if total <= 0 else max(-90.0, 20.0 * math.log10(total)))

    shares: dict[str, list[float]] = {name: [0.0] * frames for name in names}
    for i in range(frames):
        total = sum(raw[name][i] for name in names)
        if total > 0:
            for name in names:
                shares[name][i] = raw[name][i] / total

    # The coarse view, for the signals written before there were eight bands.
    # Added to `shares` only, never to `raw`: anything that sums over raw would
    # then be counting every sample twice, which is exactly how the music
    # detector quietly halved when this went from four bands to eight.
    for coarse, parts in GROUPS.items():
        shares[coarse] = [sum(shares[p][i] for p in parts) for i in range(frames)]

    # Whether each frame is a voice or turbulence. This is the one measurement
    # here that had to be verified against signals whose nature is not in
    # doubt: white noise reads 0.10, pink noise 0.17, a pure tone 0.84.
    voiced_curve = voicing(whole, frames)
    flat = [1.0 - v for v in voiced_curve]

    found = Hearing(
        window_s=1.0 / FRAME_HZ, level_db=level_db, shares=shares,
        flatness=flat, voicing=voiced_curve,
    )
    # Laughter is read off the voice bands only. A kick drum modulates the
    # envelope beautifully at exactly the wrong frequency, and it lives almost
    # entirely under 300 Hz - so it is simply not in the signal being measured.
    voiced = [
        sum(raw[b][i] for b in GROUPS["voice"] + GROUPS["upper"]) for i in range(frames)
    ]
    found.laughs = _find_laughter(voiced, FRAME_HZ)
    found.shouts = _find_shouts(level_db, shares, FRAME_HZ)
    found.drops = _find_drops(level_db, FRAME_HZ)
    found.gasps = _find_gasps(level_db, flat, shares, FRAME_HZ)
    found.sighs = _find_sighs(level_db, flat, shares, FRAME_HZ)
    found.speech_share, found.music_share = _speech_or_music(voiced, shares["bass"])
    return found


def _find_laughter(voiced: list[float], rate: float) -> list[tuple[float, float, float]]:
    """Where the voice bands pulse like a laugh, against this stream's own normal.

    The baseline comparison is not a refinement, it is the whole defence
    against music. A backing track modulates at a steady depth for as long as
    it plays; a laugh is a change. Anything that has been pulsing this way for
    the last forty-five seconds is the room, not an event in it.
    """
    span = int(ANALYSIS_S * rate)
    hop = max(1, int(HOP_S * rate))
    if len(voiced) < span:
        return []

    pulses: list[tuple[int, Pulse]] = []
    for start in range(0, len(voiced) - span + 1, hop):
        pulses.append((start, _modulation(voiced[start : start + span], rate)))

    back = max(1, int(BASELINE_S / HOP_S))
    least = max(2, int(WARMUP_S / HOP_S))
    runs: list[tuple[float, float]] = []          # (time, confidence), consecutive
    found: list[tuple[float, float, float]] = []

    for i, (start, pulse) in enumerate(pulses):
        # No history, no opinion. Without this the very first window compares
        # itself against nothing, calls itself unprecedented, and a backing
        # track scores 0.64 at t=0 on every stream that opens with music.
        if i < least:
            continue
        history = [p.depth for _, p in pulses[max(0, i - back) : i]]
        usual = _median(history) if history else 0.0

        window = voiced[start : start + span]
        mean = sum(window) / len(window)
        if mean < 0.004:                          # roughly -48 dBFS: nobody there
            continue
        if pulse.depth < MIN_DEPTH or not (LAUGH_LOW_HZ <= pulse.hz <= LAUGH_HIGH_HZ):
            continue
        if pulse.peakiness < MIN_PEAKINESS:       # smeared: talking, not laughing
            continue
        if not (0.4 <= pulse.balance <= 2.5):     # arriving or leaving, not rhythm
            continue
        lift = pulse.depth - usual
        if lift < MIN_LIFT:                       # the room has sounded like this all along
            continue

        at = start / rate
        confidence = min(1.0, (
            0.45 * min(1.0, (pulse.depth - MIN_DEPTH) / 0.35)
            + 0.30 * min(1.0, lift / 0.35)
            + 0.25 * min(1.0, (pulse.peakiness - MIN_PEAKINESS) / 3.0)
        ))
        if runs and at - runs[-1][0] > HOP_S * 1.5:
            runs.clear()
        runs.append((at, confidence))

        # Nobody laughs for a quarter of a second. Requiring the pulse to hold
        # across consecutive windows is what separates a laugh from a splice,
        # a cough, or one lucky window of a synth pad.
        if len(runs) < MIN_WINDOWS:
            continue
        start_s = runs[0][0]
        best = max(c for _, c in runs)
        if found and start_s <= found[-1][1] + 0.5:
            found[-1] = (found[-1][0], at + ANALYSIS_S, max(found[-1][2], best))
        else:
            found.append((start_s, at + ANALYSIS_S, best))
    return found


def _find_shouts(
    level_db: list[float], shares: dict[str, list[float]], rate: float,
    *, rise_db: float = 8.0, look_back_s: float = 4.0,
) -> list[tuple[float, float]]:
    """Level *and* brightness rising together - the sound of raised effort.

    Level alone is the signal that produced a clip of a betting screen with
    music over it. Vocal effort pushes energy up the spectrum, so a voice
    getting louder brightens and a fader getting pushed does not.
    """
    back = max(1, int(look_back_s * rate))
    bright = [shares["upper"][i] + shares["presence"][i] for i in range(len(level_db))]
    # A person, not a subwoofer. Without this a bass-heavy mix pumping on the
    # beat reads as somebody shouting eight times in half a minute, because
    # every kick drum is a level rise and the gap between kicks is brighter.
    human = [shares["voice"][i] + shares["upper"][i] for i in range(len(level_db))]

    found: list[tuple[float, float]] = []
    for i in range(back, len(level_db)):
        floor = _median(level_db[i - back : i])
        rise = level_db[i] - floor
        if rise < rise_db:
            continue
        if human[i] < shares["bass"][i]:
            continue
        # An absolute shift in where the energy sits, not a ratio. How big a
        # *ratio* a shout produces depends entirely on how bright the voice
        # already was, so a ratio test passes on a dark voice and fails on a
        # bright one for the same shout. The balance moving is the finding.
        was = _median(bright[i - back : i])
        if bright[i] - was < BRIGHTEN:
            continue
        at = i / rate
        if found and at - found[-1][0] < 1.0:
            found[-1] = (found[-1][0], max(found[-1][1], rise))
        else:
            found.append((at, rise))
    return found


def _find_drops(
    level_db: list[float], rate: float, *, fall_db: float = 12.0, min_s: float = 0.6
) -> list[tuple[float, float]]:
    """A loud room going abruptly quiet. The pause before a reaction, and after one."""
    back = max(1, int(3.0 * rate))
    least = max(1, int(min_s * rate))
    found: list[tuple[float, float]] = []
    i = back
    while i < len(level_db):
        was = _median(level_db[i - back : i])
        if level_db[i] > was - fall_db:
            i += 1
            continue
        end = i
        while end < len(level_db) and level_db[end] <= was - fall_db * 0.6:
            end += 1
        if end - i >= least:
            found.append((i / rate, end / rate))
        i = max(end, i + 1)
    return found


def _speech_or_music(voiced: list[float], bass_share: list[float]) -> tuple[float, float]:
    """How much of this sounds like people, and how much like a mix.

    Two cues, both about gaps. Speech stops between words - a talker's envelope
    is full of short holes - while a mix is continuous. And music carries far
    more of its energy under a few hundred Hz than a voice does.
    """
    if not voiced:
        return 0.0, 0.0
    live = _median([v for v in voiced if v > 0]) or 1e-9
    gaps = sum(1 for v in voiced if v < live * 0.35) / len(voiced)
    bass = sum(bass_share) / max(len(bass_share), 1)

    speech = max(0.0, min(1.0, gaps * 1.8)) * max(0.0, min(1.0, (0.55 - bass) / 0.35))
    music = max(0.0, min(1.0, (bass - 0.30) / 0.35)) * max(0.0, min(1.0, (0.45 - gaps) / 0.35))
    return speech, music


#: Below this a frame is turbulence rather than a voice. Placed between the
#: two clusters the measurement actually produces - noise lands at 0.10-0.17
#: and voiced sound at 0.78-0.84 - and nowhere near either of them.
#:
#: It is worth being clear about what this number is and is not. The *measure*
#: is verified: white noise 0.10, band-limited noise in breath's range 0.12, a
#: pure tone 0.84. The *thresholds below it* - how fast a gasp arrives, how
#: long a sigh runs - are reasoned from what breathing is and have never been
#: checked against a recording of anybody breathing, because there is no such
#: recording here. That is why gasps and sighs are reported and score nothing:
#: they are shown to the model that watches the clip, and what it says back is
#: the first real data either threshold will ever see.
BREATHY = 0.35
#: How fast a gasp arrives. An intake of breath is one of the fastest things a
#: person does: from nothing to full in around a tenth of a second.
GASP_ATTACK_S = 0.15
#: ...and how briefly it lasts. Longer than this and it is not a gasp, it is a
#: hiss, a fricative held for effect, or the room getting noisy.
GASP_MAX_S = 0.7
#: A sigh is the opposite shape: slow, long, and falling away.
SIGH_MIN_S = 0.7
SIGH_MAX_S = 3.0
#: How much of a sigh's energy has to be gone by the end of it. Breath out
#: decays; a held noise does not.
SIGH_DECAY = 0.6


def _find_gasps(
    level_db: list[float],
    flatness: list[float],
    shares: dict[str, list[float]],
    rate: float,
) -> list[tuple[float, float]]:
    """A sharp intake of breath: shock, a near miss, something going wrong.

    Three things at once, and each one on its own is something else entirely.

    It is **noise, not voice** - air rushing past a narrowed glottis has no
    fundamental, so the spectrum is flat where a vowel is peaky. That is what
    separates a gasp from a shout, which is the same suddenness with harmonics
    in it.

    It **arrives faster than a voice can** - a tenth of a second from nothing
    to full. Speech has to move a jaw and a tongue and cannot do that.

    And it is **over almost at once**. A breath in is followed by holding it;
    a noise that is still there a second later is a hiss, a fricative held for
    effect, or the room simply getting louder.
    """
    attack = max(1, int(GASP_ATTACK_S * rate))
    longest = max(2, int(GASP_MAX_S * rate))
    least = max(attack, int(WARMUP_S * rate))
    # Breath is mostly above the vowel range; a rumble that happens to be flat
    # is a door closing, not a person.
    breath = [shares["upper"][i] + shares["presence"][i] for i in range(len(level_db))]

    found: list[tuple[float, float]] = []
    for i in range(least, len(level_db) - longest):
        rise = level_db[i] - level_db[i - attack]
        if rise < 9.0:
            continue
        # Turbulent, not voiced. This is what separates a gasp from a shout -
        # the same suddenness, with harmonics in it.
        if flatness[i] < 1.0 - BREATHY or breath[i] < 0.45:
            continue
        # Gone again shortly afterwards - the held breath that follows it.
        after = min(level_db[i + 1 : i + longest + 1])
        if after > level_db[i] - 7.0:
            continue

        at = i / rate
        confidence = min(1.0, (
            0.4 * min(1.0, (rise - 9.0) / 12.0)
            + 0.4 * min(1.0, (flatness[i] - (1.0 - BREATHY)) / 0.25)
            + 0.2 * min(1.0, (breath[i] - 0.45) / 0.35)
        ))
        if found and at - found[-1][0] < 0.6:
            found[-1] = (found[-1][0], max(found[-1][1], confidence))
        else:
            found.append((at, confidence))
    return found


def _find_sighs(
    level_db: list[float],
    flatness: list[float],
    shares: dict[str, list[float]],
    rate: float,
) -> list[tuple[float, float]]:
    """A long breath out: exasperation, relief, giving up.

    The same turbulence as a gasp and the opposite envelope. Where a gasp is
    fast, short and followed by a hold, a sigh is slow, long and *decaying* -
    a lungful leaving over a second or more, quieter at the end than at the
    start, because that is what running out of air sounds like.

    The decay is what makes it a sigh rather than a hiss. A held noise stays
    where it is; breath out cannot.
    """
    shortest = max(2, int(SIGH_MIN_S * rate))
    longest = max(shortest + 1, int(SIGH_MAX_S * rate))
    least = max(1, int(WARMUP_S * rate))
    breath = [shares["voice"][i] + shares["upper"][i] for i in range(len(level_db))]

    found: list[tuple[float, float]] = []
    i = least
    while i < len(level_db) - shortest:
        if flatness[i] < 1.0 - BREATHY or breath[i] < 0.35:
            i += 1
            continue
        end = i
        while (
            end < len(level_db) - 1
            and end - i < longest
            and flatness[end] >= (1.0 - BREATHY) * 0.9
        ):
            end += 1
        if end - i < shortest:
            i = end + 1
            continue

        # Falling away, in amplitude rather than decibels: "sixty percent of
        # it is gone" is a statement about air, and dB is a statement about
        # perception.
        head = _median(level_db[i : i + shortest // 3])
        tail = _median(level_db[end - shortest // 3 : end])
        if 10 ** (tail / 20.0) > 10 ** (head / 20.0) * (1.0 - SIGH_DECAY):
            i = end + 1
            continue

        found.append((i / rate, end / rate))
        i = end + 1
    return found
