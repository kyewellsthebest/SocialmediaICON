#!/usr/bin/env python3
"""Find every clip in a stretch of video, and cut each one to its own edges.

    python scripts/reel.py video.mp4 --for 12:30 --cut clips/

The difference from `audition.py` is the shape of the question. That one asks
"would this thirty-second window have been cut", because that is what the live
watcher asks - it only ever sees the last half minute. This one reads the whole
stretch at once and asks "where are the clips in it", which is what a person
does, and it turns out to matter for two separate reasons:

**A baseline needs something to be a baseline of.** Every signal here is a
ratio against the stream's own recent normal, which is the right rule. But
inside a thirty-second window on a loud streamer, *everything* is the normal:
"shout" fired 11 to 17 times in every window of a real video, so it separated
nothing. Over twelve minutes the genuinely loud moments stand out from the
merely loud ones, and the same detector starts discriminating.

**A clip has edges.** See core.clipping - the old code hung a fixed 22-second
lead and 8-second trail around the trigger, so every clip opened with
twenty-two seconds of preamble and stopped eight seconds later whether
anything had resolved or not.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import clipping, moments  # noqa: E402
from core.ffmpeg_ops import require_binaries  # noqa: E402

log = logging.getLogger("reel")

#: How far apart two triggers have to be to be different moments.
APART_S = 20.0


def seconds(text: str) -> float:
    parts = [float(p) for p in str(text).split(":")]
    total = 0.0
    for part in parts:
        total = total * 60.0 + part
    return total


def mmss(s: float) -> str:
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def sense(src: Path, span_s: float, *, face_fps: float) -> tuple[dict, Any]:
    """Everything heard and seen over the whole stretch, on one grid."""
    from core import faces, hearing, watching

    signals: dict[str, Any] = {}
    heard = None
    print("  listening...", flush=True)
    try:
        heard = hearing.listen(src)
        signals |= moments.signals_from_hearing(heard, duration_s=span_s)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! hearing: {exc}", flush=True)
    print("  watching...", flush=True)
    try:
        signals |= moments.signals_from_watching(
            watching.watch(src), duration_s=span_s)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! watching: {exc}", flush=True)
    print("  looking for faces...", flush=True)
    try:
        signals |= moments.signals_from_faces(
            faces.watch(src, fps=face_fps), duration_s=span_s)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! faces: {exc}", flush=True)
    return signals, heard


def cut(src: Path, bounds: clipping.Bounds, into: Path, n: int, score: float) -> dict:
    """The clip, at its own edges, cropped to portrait the way the bot crops."""
    from core import reframe

    raw = into / f"{n:02d}-raw.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{bounds.start_s:.2f}",
         "-t", f"{bounds.length_s:.2f}", "-i", str(src),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-movflags", "+faststart", str(raw)],
        check=True, capture_output=True,
    )
    final = into / (
        f"{n:02d}-{int(bounds.start_s)//60:02d}m{int(bounds.start_s)%60:02d}s"
        f"-{bounds.length_s:.0f}s-score{score:.0f}.mp4"
    )
    framing: dict[str, Any] = {}
    reframe.to_portrait(raw, final, work_dir=into / "tmp", report=framing)
    raw.unlink(missing_ok=True)
    return {"path": final, "framing": framing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--from", dest="start", default="0")
    parser.add_argument("--for", dest="length", default="12:30")
    parser.add_argument("--most", type=int, default=25,
                        help="the most clips to consider")
    parser.add_argument("--face-fps", type=float, default=8.0)
    parser.add_argument("--cut", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    require_binaries()

    start, span = seconds(args.start), seconds(args.length)
    src = Path(args.source)
    work = Path(args.cut or ".") / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    stretch = work / "stretch.mp4"
    print(f"Taking {args.start}..{mmss(start + span)} out of {src.name}", flush=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{span:.2f}",
         "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
         "-c:a", "aac", str(stretch)],
        check=True, capture_output=True,
    )

    signals, heard = sense(stretch, span, face_fps=args.face_fps)
    print("  scoring...", flush=True)
    found = moments.rank(signals, duration_s=span, clip_s=12.0, top=args.most)

    # One trigger per moment: peaks within APART_S of each other are the same
    # thing being reported twice.
    kept: list = []
    for m in found:
        if all(abs(m.peak_s - k.peak_s) > APART_S for k in kept):
            kept.append(m)
    kept.sort(key=lambda m: m.peak_s)

    print(f"\n{len(found)} peaks, {len(kept)} distinct moments.\n")
    print(f"{'#':>2} {'clip':>14} {'len':>5} {'lead':>5} {'score':>6}  why it ends there")
    print("-" * 84)
    rows = []
    for i, m in enumerate(kept, start=1):
        b = clipping.find(heard, m.peak_s, span_s=span)
        rows.append({"n": i, "score": round(m.score, 1),
                     "why": {k: round(v, 2) for k, v in m.why.items()}, **b.as_dict()})
        print(f"{i:>2} {mmss(b.start_s) + '-' + mmss(b.end_s):>14} "
              f"{b.length_s:>4.0f}s {b.lead_s:>4.1f}s {m.score:>6.1f}  "
              f"{b.why.get('ends_on', '?')}")

    if args.cut:
        args.cut.mkdir(parents=True, exist_ok=True)
        print(f"\nCutting {len(kept)} clips into {args.cut}:", flush=True)
        for row, m in zip(rows, kept, strict=True):
            b = clipping.Bounds(row["start_s"], row["end_s"], m.peak_s)
            made = cut(stretch, b, args.cut, row["n"], m.score)
            row["file"] = made["path"].name
            row["framing"] = made["framing"].get("layout")
            print(f"  {made['path'].name}  {row['framing']}", flush=True)

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    stretch.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
