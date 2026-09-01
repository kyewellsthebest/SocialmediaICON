"""Video with a known shape, built by ffmpeg's own sources.

The point of most of these is the pair: the same event, once in a still room
and once in a nightclub. A detector that finds it in both is measuring change;
one that only finds it in the still room is measuring motion, and would clip
every second of an IRL stream forever.
"""

from __future__ import annotations

import functools
import subprocess
import tempfile
from pathlib import Path


def _build(name: str, source: str, seconds: float, out: Path) -> Path:
    path = out / f"{name}.mp4"
    if path.exists():
        return path
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-t", f"{seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(path)],
        check=True, capture_output=True,
    )
    return path


def where() -> Path:
    found = Path(tempfile.gettempdir()) / "clipengine-synth-video"
    found.mkdir(parents=True, exist_ok=True)
    return found


def _lum(expression: str) -> str:
    return f"color=c=#303030:s=640x360:r=30,geq=lum='{expression}':cb=128:cr=128"


@functools.cache
def still_room(seconds: float = 30.0) -> Path:
    """A locked-off shot of almost nothing. A man at a desk."""
    return _build("still", _lum("48+8*sin(X*0.05+T)"), seconds, where())


@functools.cache
def nightclub(seconds: float = 30.0) -> Path:
    """Heavy motion in every frame. The real one measured 0.118."""
    return _build(
        "club", _lum("60*abs(sin(X*0.3+T*25))*abs(sin(Y*0.2+T*19))+40"), seconds, where()
    )


@functools.cache
def calm_then_chaos(at: float = 20.0, seconds: float = 30.0) -> Path:
    return _build(
        "chaos",
        _lum(
            f"if(between(T,{at},{at + 4}),"
            f" 128+120*sin(X*0.5+T*40)*sin(Y*0.4+T*33), 48+8*sin(X*0.05+T))"
        ),
        seconds, where(),
    )


@functools.cache
def frozen_then_chaos(at: float = 22.0, seconds: float = 30.0) -> Path:
    """A room with *no* motion at all, then four seconds of chaos.

    Distinct from calm_then_chaos, whose "calm" still drifts - which is enough
    to give the baseline a non-zero median. This one is genuinely frozen until
    the moment, which is the case that used to be discarded: no baseline to
    divide by, so the surge was skipped entirely.
    """
    return _build(
        "frozen",
        _lum(
            f"if(between(T,{at},{at + 4}),"
            f" 128+120*sin(X*0.5+T*40)*sin(Y*0.4+T*33), 48)"
        ),
        seconds, where(),
    )


@functools.cache
def club_then_surge(at: float = 20.0, seconds: float = 30.0) -> Path:
    """The same event, buried in a stream that is already moving constantly."""
    return _build(
        "clubsurge",
        _lum(
            f"(if(between(T,{at},{at + 4}),150,60))"
            f"*abs(sin(X*0.3+T*25))*abs(sin(Y*0.2+T*19))+40"
        ),
        seconds, where(),
    )


@functools.cache
def hard_cut(at: float = 15.0, seconds: float = 30.0) -> Path:
    return _build("cut", _lum(f"if(gt(T,{at}), 230, 25)"), seconds, where())


@functools.cache
def lights_up(at: float = 12.0, seconds: float = 30.0) -> Path:
    return _build(
        "lights", _lum(f"if(gt(T,{at}), 200, 30)+10*sin(X*0.4+T*20)"), seconds, where()
    )
