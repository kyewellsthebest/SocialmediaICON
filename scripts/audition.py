#!/usr/bin/env python3
"""Run the live detector over a video you already know, and show its working.

    python scripts/audition.py stream.mp4
    python scripts/audition.py https://kick.com/video/... --at 12:30 --for 30m
    python scripts/audition.py stream.mp4 --moments 4:12,17:40 --window 20

The bot scored five hundred windows in two hours and cut one of them, off nine
streams that clip themselves for a living. Numbers like that have exactly two
explanations - the streams were quiet, or the gate is wrong - and nothing about
watching it live tells you which, because you cannot see what it declined to
cut. You can only see what it kept, and it kept nothing.

So: give it a stretch of video where *you* know where the moments are, and it
reports every window it scored, what it heard and saw in each, what the score
was, and which bar the window failed. If a moment you named scores 3, the
detector is missing it. If it scores 44 against a lone-signal bar of 55, the
detector found it and the gate threw it away. Those need opposite fixes and
look identical from the dashboard.

Two things it deliberately does not have, and both change the reading:

* **No chat.** A quarter of the evidence families are chat, and the gate wants
  two families agreeing. A window that would pass live with chat behind it can
  fail here. `--no-chat-note` suppresses the reminder; nothing else changes.
* **No model.** This is the arithmetic half only - what got a clip *cut*. What
  the model then said about it is a separate question, and by the time this
  matters you will have seen it on the Clips page.

It shares the gate with the live watcher rather than reimplementing it
(`supervisor.gate`), because a harness with its own copy of the rules tests the
harness.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import moments, supervisor  # noqa: E402
from core.config import settings  # noqa: E402
from core.ffmpeg_ops import require_binaries  # noqa: E402

log = logging.getLogger("audition")


def seconds(text: str) -> float:
    """"90", "1:30" and "1:02:03" all mean what they look like."""
    parts = [float(p) for p in str(text).split(":")]
    total = 0.0
    for part in parts:
        total = total * 60.0 + part
    return total


def span(text: str) -> float:
    """"30m", "90s", "1h" or a bare number of seconds."""
    text = str(text).strip().lower()
    scale = {"s": 1.0, "m": 60.0, "h": 3600.0}.get(text[-1:], None)
    return float(text[:-1]) * scale if scale else float(text)


@dataclass
class Window:
    """One stretch of video, scored exactly as the watcher scores one."""

    start_s: float
    end_s: float
    score: float = 0.0
    event: float = 0.0
    why: dict[str, float] = field(default_factory=dict)
    families: dict[str, float] = field(default_factory=dict)
    agreed: list[str] = field(default_factory=list)
    peak_s: float = 0.0
    passed: bool = False
    stage: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def at(self) -> str:
        return f"{int(self.peak_s) // 60}:{int(self.peak_s) % 60:02d}"


def cut_window(src: Path, start_s: float, length_s: float, into: Path) -> Path:
    """One window as its own file, so the senses read it the way they read a
    live buffer: a short stretch with nothing before it to compare against."""
    dest = into / f"w{int(start_s)}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start_s:.2f}",
         "-t", f"{length_s:.2f}", "-i", str(src),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", str(dest)],
        check=True, capture_output=True,
    )
    return dest


def read(path: Path, length_s: float) -> Window:
    """Hear it, watch it, find the faces - the three senses, then the score."""
    from core import faces, hearing, watching

    window = Window(start_s=0.0, end_s=length_s)
    signals: dict[str, Any] = {}

    try:
        heard = hearing.listen(path)
        signals |= moments.signals_from_hearing(heard, duration_s=length_s)
    except Exception as exc:  # noqa: BLE001 - a deaf window is still a reading
        window.problems.append(f"hearing: {type(exc).__name__}: {exc}")
    try:
        seen = watching.watch(path)
        signals |= moments.signals_from_watching(seen, duration_s=length_s)
    except Exception as exc:  # noqa: BLE001
        window.problems.append(f"watching: {type(exc).__name__}: {exc}")
    try:
        people = faces.watch(path)
        signals |= moments.signals_from_faces(people, duration_s=length_s)
    except Exception as exc:  # noqa: BLE001
        window.problems.append(f"faces: {type(exc).__name__}: {exc}")

    found = moments.rank(
        signals, duration_s=length_s, clip_s=supervisor.SCORE_WIDTH_S, top=1
    )
    if not found:
        return window

    best = found[0]
    window.score = best.score
    window.why = best.why
    window.peak_s = best.peak_s
    window.event = sum(v for k, v in best.why.items() if k in moments.EVENTS)
    window.families = moments.families(best.why)
    window.agreed = moments.agreeing(best.why)

    # The live gate, not a copy of it.
    scored = supervisor.Found(score=best.score, why=best.why, at_s=best.peak_s)
    window.passed, window.stage, _ = supervisor.gate(scored)
    return window


def fetch(source: str, into: Path) -> Path:
    """A local file stays where it is; anything else comes down with yt-dlp."""
    local = Path(source)
    if local.exists():
        return local

    import yt_dlp

    dest = into / "source.mp4"
    with yt_dlp.YoutubeDL({
        "outtmpl": str(dest), "quiet": True, "no_warnings": True,
        "format": "best[height<=720]/best", "merge_output_format": "mp4",
    }) as ydl:
        ydl.download([source])
    if not dest.exists():
        raise SystemExit(f"could not download {source}")
    return dest


def report(windows: list[Window], marked: list[float], *, chat_note: bool) -> None:
    passed = [w for w in windows if w.passed]
    print()
    print(f"{'at':>7}  {'score':>6} {'event':>6}  {'families':<34} verdict")
    print("-" * 78)
    for w in windows:
        shape = ", ".join(
            f"{name} {share * 100:.0f}%" for name, share in
            list(w.families.items())[:3]
        ) or "nothing"
        near = any(abs(w.peak_s - m) < 20.0 for m in marked)
        verdict = "CUT" if w.passed else w.stage
        print(f"{w.at:>7}  {w.score:>6.1f} {w.event:>6.1f}  {shape:<34} "
              f"{verdict}{'   <- you marked this' if near else ''}")
        for problem in w.problems:
            print(f"{'':>7}  !! {problem}")

    print()
    print(f"{len(windows)} windows scored, {len(passed)} would have been cut.")
    print(f"Bars: score >= {settings.live_min_score}, "
          f"event >= {settings.live_min_event_score}, "
          f"and either two families agreeing or event >= "
          f"{settings.live_lone_signal_score} on one.")

    if marked:
        print()
        for at in marked:
            near = [w for w in windows if abs(w.peak_s - at) < 20.0]
            best = max(near, key=lambda w: w.score, default=None)
            when = f"{int(at) // 60}:{int(at) % 60:02d}"
            if best is None:
                print(f"  {when}: no window covered it")
            elif best.passed:
                print(f"  {when}: FOUND, scored {best.score:.1f}")
            else:
                print(f"  {when}: MISSED at {best.score:.1f} "
                      f"(event {best.event:.1f}, {best.stage}) - "
                      f"{', '.join(best.agreed) or 'no family'}")
        print()
        print("A moment you marked that scores near zero is the detector "
              "missing it.\nOne that scores well and still says a stage name "
              "is the gate throwing it away.\nThose need opposite fixes.")

    if chat_note:
        print()
        print("No chat here, so the chat family cannot agree with anything - "
              "and the gate\nwants two families. A window failing 'one signal "
              "only' might pass live.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the live detector over a video and show its working.")
    parser.add_argument("source", help="a video file, or any URL yt-dlp can fetch")
    parser.add_argument("--at", default="0", help="start here (90, 1:30, 1:02:03)")
    parser.add_argument("--for", dest="length", default="10m",
                        help="how much of it to read (30m, 90s, 600)")
    parser.add_argument("--window", type=float, default=supervisor.SENSE_WINDOW_S,
                        help="seconds per window (default: what the watcher uses)")
    parser.add_argument("--step", type=float, default=None,
                        help="seconds between windows (default: the window size)")
    parser.add_argument("--moments", default="",
                        help="where you know the moments are: 4:12,17:40")
    parser.add_argument("--json", type=Path, help="also write the readings here")
    parser.add_argument("--no-chat-note", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    require_binaries()

    start = seconds(args.at)
    length = span(args.length)
    step = args.step or args.window
    marked = [seconds(m) for m in args.moments.split(",") if m.strip()]

    with tempfile.TemporaryDirectory(prefix="audition-") as tmp:
        work = Path(tmp)
        src = fetch(args.source, work)
        print(f"Reading {src.name} from {args.at} for {args.length}, "
              f"{args.window:.0f}s windows every {step:.0f}s.")

        windows: list[Window] = []
        at = start
        while at < start + length:
            try:
                piece = cut_window(src, at, args.window, work)
            except subprocess.CalledProcessError:
                break  # ran off the end of the video
            window = read(piece, args.window)
            # Report times against the source, not the window.
            window.start_s, window.end_s = at, at + args.window
            window.peak_s += at
            windows.append(window)
            piece.unlink(missing_ok=True)
            print(".", end="", flush=True)
            at += step

        report(windows, marked, chat_note=not args.no_chat_note)

        if args.json:
            args.json.write_text(json.dumps([
                {"start_s": w.start_s, "peak_s": w.peak_s, "score": w.score,
                 "event": w.event, "why": w.why, "families": w.families,
                 "agreed": w.agreed, "passed": w.passed, "stage": w.stage,
                 "problems": w.problems}
                for w in windows
            ], indent=2), encoding="utf-8")
            print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
